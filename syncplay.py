#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syncplay.py —— 双机同步起播（系统时钟 + 每 20 秒一个时点）

两台电脑各跑一份，彼此不通信：
  1. 双方共用同一张"时间表"：每分钟的 :00、:20 和 :40；
  2. 各自算出"下一个至少 lead 秒之后的格子"作为目标时刻 T；
  3. 在 T 时刻同时触发：FL Studio 开始播放 / 视频开始播放。

只要两台系统时钟对齐（NTP），并且启动时落在同一个格子窗口里，
两台算出的 T 就是同一个时刻。

示例
----
视频机（推荐 mpv，提前加载并暂停在首帧）：
    python syncplay.py --role video --player mpv --file "D:\\show\\video.mp4" --screen 1

FL 机：
    python syncplay.py --role fl

只演练倒计时，不碰任何软件：
    python syncplay.py --role none --dry-run --in 5

手动指定同一个时刻（最稳，适合正式演出）：
    python syncplay.py --role fl --at 20:31:00
"""

import argparse
import ctypes
import difflib
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from ctypes import wintypes

IS_WIN = (os.name == "nt")
NTP_DELTA = 2208988800          # 1900-01-01 到 1970-01-01 的秒数
def app_dir():
    """程序所在目录：打包成 exe 后是 exe 所在目录，源码运行时是脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
CLOCK_CACHE = os.path.join(HERE, "last_clock.txt")
LOG_PATH = None

# ------------------------------------------------------------------ 小工具


def fmt(ts):
    """把时间戳格式化成 HH:MM:SS.mmm"""
    return time.strftime("%H:%M:%S", time.localtime(ts)) + ".%03d" % int((ts % 1) * 1000)


def init_console():
    """让中文在 cmd / Windows Terminal 里正常显示（统一成 UTF-8）"""
    if IS_WIN:
        try:
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def log(msg):
    line = "[%s] %s" % (fmt(time.time()), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def parse_hms(text):
    """'20:31:00' 或 '20:31' -> 今天的时间戳"""
    parts = text.strip().split(":")
    if len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise SystemExit("时间格式应为 HH:MM:SS，例如 20:31:00")
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        raise SystemExit("时间不合法：%s" % text)
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, m, s, 0, 0, -1))


def parse_grid(text):
    """'0,20,40' -> [0, 20, 40]"""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if not (0 <= v < 60):
            raise SystemExit("--grid 里的秒数必须在 0-59 之间")
        out.append(v)
    if not out:
        raise SystemExit("--grid 不能为空")
    return sorted(set(out))


def suggest_file(path):
    """路径不存在时，列出同目录下最接近的文件，帮用户找错。"""
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        return "\n  目录不存在：%s" % folder
    try:
        names = [n for n in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, n))]
    except OSError:
        return ""
    base = os.path.basename(path)
    close = difflib.get_close_matches(base, names, n=3, cutoff=0.45)
    if close:
        return "\n  你是不是要：\n" + "\n".join(
            "    " + os.path.join(folder, n) for n in close)
    if names:
        return "\n  该目录下有：\n" + "\n".join("    " + n for n in names[:10])
    return "\n  该目录是空的。"


def next_slot(now, seconds, lead):
    """当前时间 -> 下一个满足 lead 的格子时刻"""
    base = math.floor(now / 60.0) * 60.0
    for k in (0, 1, 2, 3):
        for s in seconds:
            cand = base + k * 60.0 + s
            if cand - now >= lead:
                return cand
    return base + 240.0


def countdown(until, label):
    """显示倒计时并在 until 时刻返回；最后 20ms 忙等，避开 Windows 调度抖动。"""
    tty = sys.stdout.isatty()
    last_sec = None
    while True:
        remain = until - time.time()
        if remain <= 0:
            break
        if tty:
            sys.stdout.write("\r  %s %6.2f 秒   " % (label, remain))
            sys.stdout.flush()
        else:
            sec = int(math.ceil(remain))
            if sec != last_sec:
                print("  %s %d 秒" % (label, sec), flush=True)
                last_sec = sec
        if remain > 1.0:
            time.sleep(min(0.1, max(0.01, remain - 0.9)))
        elif remain > 0.02:
            time.sleep(0.004)
        else:
            while time.time() < until:
                pass
            break
    if tty:
        sys.stdout.write("\r" + " " * 36 + "\r")
        sys.stdout.flush()


# ------------------------------------------------------------------ 时钟自检


def ntp_probe(host, timeout=1.2):
    """返回 (本机相对 NTP 的偏差秒, 往返秒)；失败返回 None"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        pkt = b"\x1b" + 47 * b"\x00"
        t1 = time.time()
        s.sendto(pkt, (host, 123))
        data, _ = s.recvfrom(1024)
        t4 = time.time()
        s.close()
        if len(data) < 48:
            return None
        u = struct.unpack("!12I", data[:48])
        t2 = u[8] + u[9] / 2.0 ** 32 - NTP_DELTA    # 服务器收到
        t3 = u[10] + u[11] / 2.0 ** 32 - NTP_DELTA  # 服务器发出
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        delay = (t4 - t1) - (t3 - t2)
        return offset, delay
    except Exception:
        return None


def read_clock_cache():
    """读上次测到的时钟偏差，返回 (偏差ms, 时间戳, 服务器) 或 None。"""
    try:
        with open(CLOCK_CACHE, "r", encoding="utf-8") as f:
            parts = f.read().strip().split("|")
        ms = float(parts[0])
        ts = float(parts[1]) if len(parts) > 1 else 0.0
        server = parts[2] if len(parts) > 2 else ""
        return ms, ts, server
    except (OSError, ValueError, IndexError):
        return None


def save_clock_cache(ms, server=""):
    """把这次测到的时钟偏差存下来，供下次离线启动时使用。"""
    try:
        with open(CLOCK_CACHE, "w", encoding="utf-8") as f:
            f.write("%d|%d|%s" % (int(round(ms)), int(time.time()), server))
    except OSError:
        pass


# ------------------------------------------------------------------ Windows 输入

if IS_WIN:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL,
                                     wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         wintypes.BOOL]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_uint]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                   wintypes.DWORD, ctypes.c_void_p]
else:
    user32 = None
    kernel32 = None
    WNDENUMPROC = None

VK = {
    "SPACE": 0x20, "ENTER": 0x0D, "RETURN": 0x0D, "ESC": 0x1B, "TAB": 0x09,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "CTRL": 0x11, "CONTROL": 0x11, "SHIFT": 0x10, "ALT": 0x12,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}
for _ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    VK[_ch] = ord(_ch)


def find_window(substr):
    """按标题子串找窗口，返回 HWND 或 None"""
    if not IS_WIN:
        return None
    needle = substr.lower()
    found = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if needle in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def force_foreground(hwnd):
    """把窗口切到前台，返回是否成功"""
    if not IS_WIN or not hwnd:
        return False
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            # 置顶再取消置顶，常能抢到前台
            user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0003)
            user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, 0x0003)
            user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    return user32.GetForegroundWindow() == hwnd


def send_combo(combo):
    """发送按键，支持 CTRL+HOME 这种组合"""
    parts = [p.strip().upper() for p in combo.split("+") if p.strip()]
    if not parts:
        return
    key = parts[-1]
    if key not in VK:
        raise ValueError("不认识的按键：%s" % key)
    mods = [VK[m] for m in parts[:-1]]
    for m in mods:
        user32.keybd_event(m, 0, 0, None)
    time.sleep(0.01)
    user32.keybd_event(VK[key], 0, 0, None)
    time.sleep(0.03)
    user32.keybd_event(VK[key], 0, 2, None)
    for m in reversed(mods):
        time.sleep(0.01)
        user32.keybd_event(m, 0, 2, None)


# ------------------------------------------------------------------ 触发器


class MidiTrigger(object):
    """用 MIDI 音符触发 FL 的 Play（比模拟按键稳）"""

    def __init__(self, port, note, channel):
        try:
            import mido
        except ImportError:
            raise SystemExit("MIDI 触发需要 mido：pip install mido python-rtmidi")
        self._mido = mido
        self._out = mido.open_output(port)
        self._note = note
        self._ch = max(0, min(15, channel - 1))

    def fire(self):
        self._out.send(self._mido.Message("note_on", note=self._note,
                                          velocity=127, channel=self._ch))
        time.sleep(0.02)
        self._out.send(self._mido.Message("note_off", note=self._note,
                                          velocity=0, channel=self._ch))


def fl_fire(cfg, midi, hwnd=None):
    """返回是否成功发出触发。hwnd 是准备阶段缓存的窗口，避免触发时再枚举（省 ~10ms）"""
    if midi is not None:
        midi.fire()
        return True
    if not IS_WIN:
        log("!! FL 触发只支持 Windows")
        return False
    if hwnd and user32.IsWindow(hwnd):
        if user32.GetForegroundWindow() != hwnd:
            force_foreground(hwnd)
        send_combo(cfg.fl_keys)
        return True
    hwnd = find_window(cfg.fl_window)
    if not hwnd:
        log("!! 找不到标题包含 %r 的窗口，按键没有发送" % cfg.fl_window)
        return False
    ok = force_foreground(hwnd)
    if not ok:
        # 再抢一次
        time.sleep(0.02)
        ok = force_foreground(hwnd)
    if not ok:
        log("!! 无法把 FL 切到前台，按键可能发到别的窗口")
    send_combo(cfg.fl_keys)
    return True


class MpvPlayer(object):
    """mpv：启动即加载并暂停在首帧，到点通过 IPC 取消暂停"""

    def __init__(self, cfg, idle=False):
        self._conn = None
        self._pipe_path = cfg.mpv_pipe
        self._duration = None       # 视频时长（秒），load 后查询一次
        self._duration_tries = 0
        self._start_time = None     # 起播时刻（monotonic），用于估算播完
        self._req_id = 0
        # 启动前先清掉遗留的 mpv，避免旧窗口挡屏、旧进程占着 IPC 管道
        n_closed = close_existing_mpv()
        if n_closed:
            log("已关闭 %d 个残留的 mpv 进程" % n_closed)
        base = [cfg.mpv, "--no-config", "--no-resume-playback",
                "--save-position-on-quit=no", "--input-ipc-server=" + cfg.mpv_pipe,
                "--pause=yes", "--start=0", "--keep-open=yes",
                "--force-window=yes", "--no-osc", "--input-vo-keyboard=no",
                "--really-quiet"]
        if idle:
            base.append("--idle=yes")
        if getattr(cfg, "stretch", False):
            base.append("--keepaspect=no")   # 拉伸铺满，不保持宽高比
        if not cfg.no_fullscreen:
            base += ["--fs", "--fs-screen=%d" % cfg.screen]
        if not idle:
            base.append(cfg.file)

        # 不同 mpv 版本这个选项名字不一样：0.42 起叫 focus-on，旧版叫 focus-on-open。
        # 依次尝试，哪个能活下来用哪个；都不行就不带这个选项。
        self.proc = None
        for opt in ("--focus-on=never", "--focus-on-open=no", ""):
            args = list(base) + ([opt] if opt else [])
            proc = subprocess.Popen(args)
            time.sleep(0.2)
            if proc.poll() is None:
                self.proc = proc
                break
        if self.proc is None:
            raise RuntimeError("mpv 启动失败：当前版本不认识启动参数")
        self._connect()

    def _connect(self, timeout=10.0):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                self._conn = open(self._pipe_path, "r+b", buffering=0)
                return
            except OSError as e:
                last = e
                time.sleep(0.05)
        raise RuntimeError("连不上 mpv 的 IPC 管道（mpv 是否在运行？）：%s" % last)

    def _cmd(self, *cmd):
        data = (json.dumps({"command": list(cmd)}) + "\n").encode("utf-8")
        try:
            self._conn.write(data)
        except OSError:
            self._connect(2.0)
            self._conn.write(data)

    def get_property(self, name, default=None):
        """同步查询 mpv 属性：写一条命令，读到对应应答为止。

        注意：这里刻意不启动后台读线程。Windows 同步命名管道句柄上，
        一个线程阻塞在 read 时会把另一个线程的 write 一起卡住（界面假死），
        所以只在必要时做一次请求/响应，平时不常驻读管道。
        """
        self._req_id += 1
        rid = self._req_id
        data = (json.dumps({"command": ["get_property", name],
                            "request_id": rid}) + "\n").encode("utf-8")
        try:
            self._conn.write(data)
        except OSError:
            try:
                self._connect(2.0)
                self._conn.write(data)
            except Exception:
                return default
        while True:
            try:
                line = self._conn.readline()
            except Exception:
                return default
            if not line:
                return default
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if obj.get("request_id") == rid:
                if obj.get("error") != "success":
                    return default
                return obj.get("data")

    def ensure_duration(self):
        """查询并缓存视频时长（秒）；最多重试若干次，拿不到就返回 None。"""
        if self._duration is None and self._duration_tries < 20:
            self._duration_tries += 1
            value = self.get_property("duration")
            if isinstance(value, (int, float)) and value > 0:
                self._duration = float(value)
        return self._duration

    def reached_eof(self):
        """是否已播放到结尾（按 duration + 起播时刻估算，避免并发读管道）。"""
        if self._start_time is None:
            return False
        if self._duration is None:
            self.ensure_duration()
        if not self._duration:
            return False
        return (time.monotonic() - self._start_time) >= self._duration + 0.1

    def _reset_eof(self):
        self._start_time = None

    def fire(self):
        self._start_time = time.monotonic()
        self._cmd("set_property", "pause", False)

    def set_keepaspect(self, keep):
        """运行时切换是否保持宽高比（keep=False 即拉伸铺满）。"""
        self._cmd("set_property", "keepaspect", bool(keep))

    def load(self, path):
        """往已经开着的 mpv 里换片（比重新启动进程快得多）"""
        self._reset_eof()
        self._duration = None
        self._duration_tries = 0
        self._cmd("set_property", "pause", True)
        self._cmd("loadfile", path, "replace")
        self._cmd("set_property", "pause", True)
        self._cmd("seek", 0, "absolute")

    def reset(self):
        """回到起点并暂停，供下一次就位使用（播完后 mpv 停在最后一帧）"""
        self._start_time = None
        self._cmd("set_property", "pause", True)
        self._cmd("seek", 0, "absolute")

    def close(self):
        try:
            if self._conn:
                self._conn.close()
        except OSError:
            pass

    def stop(self):
        self.close()
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


class VlcPlayer(object):
    """VLC：--start-paused 加载，到点用 rc 接口取消暂停"""

    def __init__(self, cfg):
        self._addr = ("127.0.0.1", cfg.vlc_rc_port)
        args = [cfg.vlc, "--start-paused", "--no-video-title-show", "--no-osd",
                "--extraintf", "rc", "--rc-host", "%s:%d" % self._addr]
        if not cfg.no_fullscreen:
            args.append("--fullscreen")
        args.append(cfg.file)
        self.proc = subprocess.Popen(args)
        self._wait_rc()

    def _wait_rc(self, timeout=12.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                s = socket.create_connection(self._addr, 0.3)
                s.close()
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("VLC 的 rc 接口没有起来（端口 %d）" % self._addr[1])

    def fire(self):
        with socket.create_connection(self._addr, 1.0) as s:
            s.sendall(b"pause\n")

    def reset(self):
        try:
            with socket.create_connection(self._addr, 1.0) as s:
                s.sendall(b"stop\n")
                s.sendall(b"play\n")
                time.sleep(0.08)
                s.sendall(b"pause\n")
        except OSError:
            pass

    def close(self):
        pass

    def stop(self):
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:
            pass


class WmpPlayer(object):
    """Windows Media Player（COM）。精度最差，仅作兼容。"""

    def __init__(self, cfg):
        try:
            import win32com.client
        except ImportError:
            raise SystemExit("WMP 触发需要 pywin32：pip install pywin32")
        self._wmp = win32com.client.Dispatch("WMPlayer.OCX")
        try:
            self._wmp.settings.autoStart = False
        except Exception:
            pass
        self._wmp.URL = os.path.abspath(cfg.file)
        self._wmp.controls.play()
        time.sleep(0.2)
        self._wmp.controls.pause()
        try:
            self._wmp.controls.currentPosition = 0
        except Exception:
            pass
        if not cfg.no_fullscreen:
            try:
                self._wmp.fullScreen = True
            except Exception:
                pass

    def fire(self):
        self._wmp.controls.play()

    def reset(self):
        try:
            self._wmp.controls.currentPosition = 0
            self._wmp.controls.pause()
        except Exception:
            pass

    def close(self):
        pass

    def stop(self):
        try:
            self._wmp.controls.stop()
        except Exception:
            pass


PLAYER_PATHS = {
    "mpv": [
        r"%LOCALAPPDATA%\Microsoft\WinGet\Links\mpv.exe",
        r"%LOCALAPPDATA%\Programs\mpv\mpv.exe",
        r"%LOCALAPPDATA%\Programs\MPV Player\mpv.exe",
        r"%ProgramFiles%\mpv\mpv.exe",
        r"%ProgramFiles(x86)%\mpv\mpv.exe",
        r"%ProgramFiles%\MPV Player\mpv.exe",
        r"%ProgramFiles(x86)%\MPV Player\mpv.exe",
        r"%USERPROFILE%\scoop\shims\mpv.exe",
        r"C:\ProgramData\chocolatey\bin\mpv.exe",
    ],
    "vlc": [
        r"%ProgramFiles%\VideoLAN\VLC\vlc.exe",
        r"%ProgramFiles(x86)%\VideoLAN\VLC\vlc.exe",
    ],
}


# 随包自带的播放器（打包版优先用这些，保证行为和开发时一致）
BUNDLED_PATHS = {
    "mpv": [
        os.path.join(HERE, "mpv", "mpv.exe"),
        os.path.join(HERE, "mpv.exe"),
    ],
}


def resolve_exe(player, configured):
    """在 PATH 和常见安装位置里找播放器，找不到返回 None。"""
    if os.path.isfile(configured):
        return configured
    for cand in BUNDLED_PATHS.get(player, []):     # 随包自带的优先
        if os.path.isfile(cand):
            return cand
    has_dir = os.sep in configured or (os.altsep is not None and os.altsep in configured)
    if not has_dir:
        found = shutil.which(configured)
        if found:
            return found
    for cand in PLAYER_PATHS.get(player, []):
        path = os.path.expandvars(cand)
        if os.path.isfile(path):
            return path
    return None


def missing_player_msg(player):
    if player == "mpv":
        return ("找不到 mpv（视频播放器），不是视频文件的问题。\n"
                "  1) 安装：winget install -e --id shinchiro.mpv\n"
                "  2) 装完关掉当前终端，重新打开（PATH 才会生效）\n"
                "  3) 或者用 --mpv \"C:\\...\\mpv.exe\" 指定完整路径")
    return ("找不到 VLC（视频播放器），不是视频文件的问题。\n"
            "  1) 安装：winget install -e --id VideoLAN.VLC\n"
            "  2) 装完关掉当前终端，重新打开\n"
            "  3) 或者用 --vlc \"C:\\...\\vlc.exe\" 指定完整路径")


def close_existing_mpv():
    """关闭已经在运行的 mpv（清掉上次遗留的窗口/进程，释放 IPC 管道）。"""
    if not IS_WIN:
        return 0
    closed = 0
    for name in ("mpv.exe", "mpv.com"):
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=0x08000000)
            if res.returncode == 0:
                closed += 1
        except Exception:
            pass
    if closed:
        time.sleep(0.3)          # 等窗口关闭 / 管道释放
    return closed


def make_player(cfg, idle=False):
    try:
        if cfg.player == "mpv":
            exe = resolve_exe("mpv", cfg.mpv)
            if not exe:
                raise SystemExit(missing_player_msg("mpv"))
            if exe != cfg.mpv:
                log("自动找到 mpv：%s" % exe)
            cfg.mpv = exe
            return MpvPlayer(cfg, idle=idle)
        if cfg.player == "vlc":
            exe = resolve_exe("vlc", cfg.vlc)
            if not exe:
                raise SystemExit(missing_player_msg("vlc"))
            if exe != cfg.vlc:
                log("自动找到 VLC：%s" % exe)
            cfg.vlc = exe
            return VlcPlayer(cfg)
        if cfg.player == "wmp":
            return WmpPlayer(cfg)
    except FileNotFoundError as e:
        raise SystemExit("启动播放器失败：%s" % e)
    raise SystemExit("未知播放器：%s" % cfg.player)


# ------------------------------------------------------------------ 参数


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="双机同步起播：共用 :00/:20/:40 时间表，各自在同一个 T 触发。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python syncplay.py --role fl\n"
            "  python syncplay.py --role video --player mpv --file D:\\show\\v.mp4 --screen 1\n"
            "  python syncplay.py --role fl --at 20:31:00\n"
            "  python syncplay.py --role none --dry-run --in 5\n"
        ))
    p.add_argument("--role", choices=["none", "fl", "video", "both"], default="none",
                   help="本机负责触发什么（默认 none，只倒计时）")
    p.add_argument("--at", help="目标时刻 HH:MM:SS，两台必须填同一个值")
    p.add_argument("--in", dest="in_sec", type=float, metavar="N",
                   help="从现在起 N 秒后触发（排练用）")
    p.add_argument("--grid", default="0,10,20,30,40,50",
                   help="每分钟的秒数网格，默认 0,10,20,30,40,50（10 秒一个时点）")
    p.add_argument("--lead", type=float, default=8.0,
                   help="目标时刻至少距现在多少秒，默认 8（也是两台的对齐窗口）")
    p.add_argument("--prepare-lead", type=float, default=3.0,
                   help="提前多少秒做触发前准备，默认 3")
    p.add_argument("--offset-ms", type=float, default=0.0,
                   help="本机补偿：正数=晚触发，负数=早触发")
    p.add_argument("--late-tolerance", type=float, default=0.5,
                   help="超过目标多少秒就放弃本次触发，默认 0.5")
    p.add_argument("--yes", action="store_true", help="跳过待命确认")
    p.add_argument("--dry-run", action="store_true", help="只倒计时，不启动/不触发任何软件")
    p.add_argument("--log", default="", help="日志文件路径，默认不写")
    p.add_argument("--ntp-server", default="ntp.aliyun.com", help="启动时做时钟自检的 NTP 服务器")
    p.add_argument("--no-ntp", action="store_true", help="跳过时钟自检")
    p.add_argument("--ntp-warn-ms", type=float, default=50.0, help="偏差超过多少毫秒时告警")

    g = p.add_argument_group("FL Studio")
    g.add_argument("--fl-window", default="FL Studio", help="FL 窗口标题包含的文字")
    g.add_argument("--fl-keys", default="SPACE", help="触发 FL 的按键，默认 SPACE")
    g.add_argument("--fl-midi-port", default="", help="用 MIDI 触发时的虚拟端口名（更稳）")
    g.add_argument("--fl-midi-note", type=int, default=60, help="MIDI 音符编号，默认 60")
    g.add_argument("--fl-midi-channel", type=int, default=1, help="MIDI 通道，默认 1")

    v = p.add_argument_group("视频播放器")
    v.add_argument("--player", choices=["mpv", "vlc", "wmp"], default="mpv")
    v.add_argument("--file", default="", help="视频文件路径")
    v.add_argument("--screen", type=int, default=1, help="mpv 全屏输出到第几块屏，0 是第一块")
    v.add_argument("--no-fullscreen", action="store_true", help="不要全屏")
    v.add_argument("--stretch", action="store_true",
                   help="拉伸画面铺满全屏（不保持宽高比）")
    v.add_argument("--mpv", default="mpv", help="mpv 可执行文件路径")
    v.add_argument("--mpv-pipe", default=r"\\.\pipe\syncplay-mpv", help="mpv IPC 管道名")
    v.add_argument("--vlc", default="vlc", help="VLC 可执行文件路径")
    v.add_argument("--vlc-rc-port", type=int, default=4212, help="VLC rc 端口")
    v.add_argument("--hold", action="store_true", help="触发后保持脚本运行（WMP 需要）")
    return p.parse_args(argv)


# ------------------------------------------------------------------ 主流程


def main(argv=None):
    global LOG_PATH
    init_console()
    cfg = parse_args(argv)
    LOG_PATH = cfg.log or None

    if cfg.at and cfg.in_sec is not None:
        raise SystemExit("--at 和 --in 只能二选一")
    if cfg.role in ("fl", "both") and not IS_WIN and not cfg.dry_run:
        raise SystemExit("FL 触发只支持 Windows")
    if cfg.role in ("video", "both") and not cfg.dry_run:
        if not cfg.file:
            raise SystemExit("--role video/both 需要 --file")
        if not os.path.isfile(cfg.file):
            raise SystemExit("视频文件不存在：%s%s" % (cfg.file, suggest_file(cfg.file)))

    seconds = parse_grid(cfg.grid)

    # 1. 时钟自检
    if not cfg.no_ntp:
        probe = ntp_probe(cfg.ntp_server)
        if probe:
            off, rtt = probe
            log("时钟自检：本机与 %s 相差 %+.1f ms（往返 %.1f ms）"
                % (cfg.ntp_server, off * 1000.0, rtt * 1000.0))
            if abs(off) * 1000.0 > cfg.ntp_warn_ms:
                log("!! 偏差超过 %.0f ms，建议先执行：w32tm /resync" % cfg.ntp_warn_ms)
        else:
            log("时钟自检失败（NTP 被网络拦截？），跳过；请自己确认两台已对时")

    # 2. 预加载播放器 / MIDI
    player = None
    midi = None
    fl_hwnd = None
    try:
        # 先确认再拉起全屏播放器，免得窗口盖住控制台
        if not cfg.yes:
            try:
                input("\n确认 FL 已停止并定位到起点、视频文件已就位，按 Enter 待命... ")
            except EOFError:
                pass

        if not cfg.dry_run:
            if cfg.role in ("video", "both"):
                player = make_player(cfg)
                log("播放器已加载并暂停：%s" % cfg.player)
            if cfg.role in ("fl", "both") and cfg.fl_midi_port:
                midi = MidiTrigger(cfg.fl_midi_port, cfg.fl_midi_note, cfg.fl_midi_channel)
                log("MIDI 触发已就绪：%s" % cfg.fl_midi_port)

        # 3. 目标时刻
        now = time.time()
        if cfg.at:
            target = parse_hms(cfg.at)
            if target <= now:
                raise SystemExit("目标时刻 %s 已经过了" % cfg.at)
        elif cfg.in_sec is not None:
            if cfg.in_sec <= 0:
                raise SystemExit("--in 必须大于 0")
            target = now + cfg.in_sec
        else:
            target = next_slot(now, seconds, cfg.lead)
        if target - now > 6 * 3600:
            raise SystemExit("目标时刻超过 6 小时后，先确认时间没填错")

        eff = target + cfg.offset_ms / 1000.0

        # 4. 打印目标，两台必须一致
        log("=" * 54)
        log(" 时间表          = 每分钟 %s 秒" % "/".join(str(s) for s in seconds))
        log(" 目标起播时刻 T  = %s" % fmt(target))
        log(" 本机角色        = %s" % cfg.role)
        log(" 本机补偿        = %+.0f ms" % cfg.offset_ms)
        if abs(cfg.offset_ms) > 0.001:
            log(" 实际等待到      = %s" % fmt(eff))
        log("=" * 54)
        log(" 另一台机器必须显示同一个 T；不一致就重开一台。")

        # 5. 准备阶段
        prep_at = eff - cfg.prepare_lead
        if prep_at > time.time():
            countdown(prep_at, "距准备")
        if not cfg.dry_run and cfg.role in ("fl", "both") and midi is None:
            fl_hwnd = find_window(cfg.fl_window)
            if fl_hwnd:
                log("把 FL 切到前台：%s" % ("成功" if force_foreground(fl_hwnd) else "失败"))
            else:
                log("!! 找不到 FL 窗口：%s" % cfg.fl_window)

        # 6. 等到 T 并触发
        countdown(eff, "距起播")
        late = time.time() - eff
        if late > cfg.late_tolerance:
            log("!! 已经晚了 %.3f 秒，放弃本次触发（避免只有一台开播）" % late)
            return 2

        if cfg.dry_run:
            log("[dry-run] 这里会触发：%s" % cfg.role)
            return 0

        t0 = time.time()
        fired = True
        if cfg.role in ("fl", "both"):
            fired = fl_fire(cfg, midi, fl_hwnd) and fired
        if player is not None:
            player.fire()
        t1 = time.time()
        if fired:
            log("已触发：调用耗时 %.1f ms，相对 T 偏差 %+.1f ms"
                % ((t1 - t0) * 1000.0, (t0 - eff) * 1000.0))
        else:
            log("!! 触发未完成，看上面的告警；调用耗时 %.1f ms" % ((t1 - t0) * 1000.0))
        if cfg.offset_ms:
            log("提示：本机带了 %+.0f ms 补偿" % cfg.offset_ms)

        hold = cfg.hold or (player is not None and cfg.player == "wmp")
        if hold and player is not None:
            try:
                input("播放中，按 Enter 结束脚本（不会停止播放）... ")
            except EOFError:
                pass
        return 0
    finally:
        if player is not None:
            player.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(130)
