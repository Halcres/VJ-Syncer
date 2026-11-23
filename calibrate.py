# -*- coding: utf-8 -*-
"""对时校准工具：按系统时钟连续播放“四拍一组”的节拍，两台一起听、边听边调补偿。

设计要点：
- 音频是提前渲染好的 4 秒循环 WAV，用 SND_LOOP 连续播放，
  不再“每拍调一次 PlaySound”，所以不会因为设备忙/休眠而漏拍；
- 屏幕上的拍号由系统时钟推算（和音频同相位），并且常显不闪，
  不会出现“数字一闪就没了”的情况；
- 补偿值写入 last_offset.txt，播放端会自动读取。
"""

import math
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

try:
    import winsound
except ImportError:          # 非 Windows
    winsound = None

import syncplay as sp

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "v1.5.1"
OFFSET_FILE = os.path.join(HERE, "last_offset.txt")
NTP_HOST = "ntp.aliyun.com"

BG = "#0E1116"
PANEL = "#161B22"
CARD = "#151A21"
BORDER = "#2A313C"
TEXT = "#E9EEF5"
MUTED = "#8B95A5"
DIM = "#5C6675"
ACCENT = "#4CC2FF"
GREEN = "#3DD68C"
RED = "#FF6B6B"
DARK_TEXT = "#04121A"

GROUP = 4              # 四拍一组
BEAT_FREQ = 1200
BEAT_MS = 40
ACCENT_FREQ = 1600
ACCENT_MS = 70

_LOOP_PATHS = {}


def loop_wav(phase, rate=44100):
    """生成 4 秒循环 WAV；phase = 起播那一秒在组内的位置（0..3）。

    重音固定落在“整秒 % 4 == 0”的那一拍上，所以不同起播相位要用不同的
    文件（把重音放在循环里的对应位置），这样屏幕上的「1」永远对着重音。
    """
    phase %= GROUP
    path = _LOOP_PATHS.get(phase)
    if path and os.path.isfile(path):
        return path
    import struct
    total = rate * GROUP
    pcm = bytearray(total * 2)
    accent_at = (-phase) % GROUP       # 循环里第几拍是重音
    for k in range(GROUP):
        accent = (k == accent_at)
        freq = ACCENT_FREQ if accent else BEAT_FREQ
        dur = ACCENT_MS if accent else BEAT_MS
        amp = 24000 if accent else 22000
        n = max(1, int(rate * dur / 1000.0))
        base = k * rate
        for i in range(n):
            env = (1.0 - i / float(n)) ** 3
            v = int(amp * env * math.sin(2 * math.pi * freq * i / rate))
            p = (base + i) * 2
            pcm[p:p + 2] = struct.pack("<h", v)
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(pcm)) + bytes(pcm)
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    path = os.path.join(tempfile.gettempdir(), "syncer_beat_loop_%d.wav" % phase)
    try:
        with open(path, "wb") as f:
            f.write(wav)
        _LOOP_PATHS[phase] = path
    except OSError:
        return ""
    return path


def read_offset():
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0


def save_offset(value):
    try:
        with open(OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write("%d" % int(round(value)))
    except OSError:
        pass


def enable_dpi():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def pick_font(root, names):
    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    for n in names:
        if n in available:
            return n
    return names[-1]


class CalibApp(object):
    def __init__(self, root):
        self.root = root
        self.offset = read_offset()
        self._stop = None
        self._thread = None
        self._restart = False
        self._last_pos = None
        self._loop_start = None
        self._clock_ms = None
        self._clock_cached = False
        self._clock_cached_ts = 0.0
        self._probe_q = queue.Queue()
        self.ui = pick_font(root, ["Microsoft YaHei UI", "微软雅黑",
                                   "Microsoft YaHei", "Segoe UI"])
        self.mono = pick_font(root, ["Consolas", "Cascadia Mono", "Courier New"])
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._probe_ntp()
        self.start_beats()          # 打开即自动发声
        self.upkeep()

    # ---------------------------------------------------------------- 界面

    def _build(self):
        ui, mono = self.ui, self.mono
        tk.Label(self.root, text="对时校准  " + VERSION, font=(ui, 15, "bold"),
                 fg=TEXT, bg=BG).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(self.root,
                 text="打开即自动播放：每 4 拍一组，第一拍音更高更长、屏幕显示「1」。\n"
                      "两台一起听：重音不重合或有双击声，就拖下面滑块对齐。负数 = 提前。",
                 font=(ui, 9), fg=MUTED, bg=BG, justify="left").pack(
            anchor="w", padx=18, pady=(0, 10))

        self.flash = tk.Label(self.root, text="", bg=CARD, height=3,
                              font=(mono, 26, "bold"), fg=TEXT)
        self.flash.pack(fill="x", padx=18)

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=18, pady=(8, 0))
        self.now_lbl = tk.Label(row, text="--:--:--", font=(mono, 11),
                                fg=TEXT, bg=BG)
        self.now_lbl.pack(side="left")
        self.next_lbl = tk.Label(row, text="", font=(mono, 11), fg=ACCENT, bg=BG)
        self.next_lbl.pack(side="right")

        tk.Label(self.root, text="补偿", font=(ui, 9), fg=MUTED,
                 bg=BG).pack(anchor="w", padx=18, pady=(10, 0))
        self.scale = tk.Scale(
            self.root, from_=-300, to=300, resolution=1, orient="horizontal",
            command=self._on_slide, bg=BG, fg=TEXT, troughcolor=CARD,
            highlightthickness=0, bd=0, sliderrelief="flat",
            font=(mono, 9), length=380)
        self.scale.set(int(round(self.offset)))
        self.scale.pack(fill="x", padx=18)
        self.off_lbl = tk.Label(self.root, text=self._off_text(),
                                font=(mono, 11, "bold"), fg=ACCENT, bg=BG)
        self.off_lbl.pack(anchor="w", padx=18)

        self.ntp_lbl = tk.Label(self.root, text="时钟：检测中…（没联网就忽略）",
                                font=(ui, 9), fg=DIM, bg=BG)
        self.ntp_lbl.pack(anchor="w", padx=18, pady=(4, 10))

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=18, pady=(0, 12))
        tk.Button(btns, text="清零", command=self.reset_offset,
                  font=(ui, 9, "bold"), bg=PANEL, fg=TEXT,
                  activebackground="#202833", activeforeground=TEXT,
                  relief="flat", bd=0, highlightthickness=1,
                  highlightbackground=BORDER, padx=14, pady=6,
                  cursor="hand2").pack(side="left")

        tk.Label(self.root,
                 text="补偿写入 last_offset.txt，播放端会自动读取。",
                 font=(ui, 8), fg=DIM, bg=BG).pack(anchor="w", padx=18,
                                                   pady=(0, 14))

    def _off_text(self):
        return "补偿 %+d ms" % int(round(self.offset))

    def _on_slide(self, value):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.offset = v
        save_offset(v)
        self.off_lbl.config(text=self._off_text())
        self._restart = True          # 让音频在下一次整秒重新对齐相位

    def reset_offset(self):
        self.scale.set(0)             # 触发 _on_slide
        self._on_slide("0")

    # ---------------------------------------------------------------- 节拍

    def start_beats(self):
        """打开即开始发声，不提供停止按钮。"""
        if self._stop is not None or winsound is None:
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat_loop,
                                        args=(self._stop,), daemon=True)
        self._thread.start()

    def _pad(self):
        """节拍相对整秒的偏移（秒）：手动补偿 + 系统时钟偏差的自动补偿。"""
        pad = self.offset / 1000.0
        cm = self._clock_ms
        if cm is not None and 40.0 < abs(cm) <= 2000.0:
            pad -= cm / 1000.0        # 本机落后(cm>0) → 提前触发
        return pad

    def _beat_loop(self, stop):
        while not stop.is_set():
            off = self._pad()
            now = time.time()
            # 趁到下一个“整秒（相对补偿后）”的瞬间起播
            sec = int(math.floor(now - off)) + 1
            target = sec + off
            if target - now < 0.02:
                sec += 1
                target = sec + off
            phase = sec % GROUP
            path = loop_wav(phase)
            if not path:
                return
            self._loop_start = target
            while not stop.is_set():
                remain = target - time.time()
                if remain <= 0.02:
                    break
                time.sleep(min(0.01, max(0.002, remain - 0.01)))
            if stop.is_set():
                break
            while time.time() < target and not stop.is_set():
                pass
            if stop.is_set():
                break
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME
                                   | winsound.SND_ASYNC | winsound.SND_LOOP)
            except Exception:
                pass
            # 循环播放中；只有改补偿或退出时才重启/停止
            while not stop.is_set() and not self._restart:
                time.sleep(0.05)
            self._restart = False
            try:
                winsound.PlaySound(None, 0)
            except Exception:
                pass

    def _on_close(self):
        if self._stop is not None:
            self._stop.set()
        try:
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------------------------------------------------------------- 时钟自检

    def _probe_ntp(self):
        def work():
            best = None
            for _ in range(3):
                r = sp.ntp_probe(NTP_HOST)
                if r and (best is None or r[1] < best[1]):
                    best = r
            self._probe_q.put(best)

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------------- 主循环

    def _flash(self, pos):
        try:
            if pos == 0:                     # 每组第一拍：亮蓝 + 显示 1
                self.flash.config(bg=ACCENT, fg=DARK_TEXT, text="1")
            else:
                self.flash.config(bg="#22303C", fg=TEXT, text=str(pos + 1))
        except Exception:
            pass

    def upkeep(self):
        try:
            best = self._probe_q.get_nowait()
        except queue.Empty:
            best = False
        if best is False:
            pass
        elif best:
            new_ms = best[0] * 1000.0
            if self._clock_ms is None or abs(new_ms - self._clock_ms) > 5.0:
                self._clock_ms = new_ms
                self._clock_cached = False
                self._restart = True      # 时钟补偿变了，重新对齐循环相位
            sp.save_clock_cache(new_ms, NTP_HOST)
        else:
            # 探测失败（没联网）：退回上次保存的偏差
            cached = sp.read_clock_cache()
            if cached is not None:
                self._clock_ms = cached[0]
                self._clock_cached = True
                self._clock_cached_ts = cached[1]
                self._restart = True
            elif self._clock_ms is not None:
                self._clock_ms = None
                self._restart = True

        now = time.time()
        self.now_lbl.config(text=time.strftime("%H:%M:%S", time.localtime(now)))

        # 拍号由“整秒 + 补偿”推算，与音频同相位；常显不闪
        pad = self._pad()
        t = now - pad
        sec = int(math.floor(t))
        pos = sec % GROUP
        if pos != self._last_pos:
            self._last_pos = pos
            self._flash(pos)

        nxt = math.floor(t) + 1.0 + pad
        next_pos = (sec + 1) % GROUP + 1
        self.next_lbl.config(
            text="下一拍 %.2f 秒 · 第 %d 拍" % (max(0.0, nxt - now), next_pos))

        if self._clock_ms is not None:
            cm = self._clock_ms
            if self._clock_cached:
                when = ""
                if self._clock_cached_ts:
                    when = time.strftime(
                        "%m-%d", time.localtime(self._clock_cached_ts))
                self.ntp_lbl.config(
                    text="本机与 NTP 偏差 %+.0f ms（离线，用%s测得值）"
                         % (cm, when or "上次"), fg=DIM)
            elif 40.0 < abs(cm) <= 2000.0:
                self.ntp_lbl.config(
                    text="本机与 NTP 偏差 %+.0f ms（已自动补偿）" % cm, fg=DIM)
            else:
                self.ntp_lbl.config(
                    text="本机与 NTP 偏差 %+.0f ms" % cm,
                    fg=(RED if abs(cm) > 50 else DIM))

        self.root.after(30, self.upkeep)


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    enable_dpi()
    root = tk.Tk()
    root.title("对时校准  " + VERSION)
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    CalibApp(root)
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                   max(0, (sh - h) // 4)))
    root.minsize(w, h)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
