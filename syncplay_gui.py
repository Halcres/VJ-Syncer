#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syncplay_gui.py —— 图形界面版双机同步起播

界面只做三件事：
  1. 显示"下一时点"和"下下个时点"；
  2. 按 Enter 就位，倒计时到 0 时触发；
  3. 按 Esc 取消。

用法（和命令行版参数一致）：
    python syncplay_gui.py --role fl
    python syncplay_gui.py --role video --player mpv --file "D:\show\v.mp4" --screen 1
    python syncplay_gui.py --role none --dry-run      # 只演练界面
"""

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import messagebox

import syncplay as sp

BG = "#0E1116"
PANEL = "#161B22"
CARD = "#151A21"
BORDER = "#2A313C"
TEXT = "#E9EEF5"
MUTED = "#8B95A5"
DIM = "#5C6675"
ACCENT = "#4CC2FF"
GREEN = "#3DD68C"
AMBER = "#FFB020"
RED = "#FF6B6B"
DARK_TEXT = "#04121A"
HINT_KEY = "#010101"    # 右下角提示的"透明色"（用 transparentcolor 抠掉背景）

GUI_LEAD = 3.0          # 下一个时点至少还有这么多秒才用它
FL_RESET_DELAY = 1.0    # FL 端起播后多久自动复位（不用按 Esc）
PROBE_INTERVAL = 10 * 60 * 1000     # 时钟偏差自动重测间隔：10 分钟
PROBE_RETRY = 60 * 1000             # 倒计时/演出中推迟重测的间隔

# 时间表间隔选项：10 秒是默认；时点更密，对齐窗口也更小
GRID_CHOICES = [
    ((0, 10, 20, 30, 40, 50), "10 秒（默认）"),
    ((0, 15, 30, 45), "15 秒"),
    ((0, 20, 40), "20 秒"),
]

HERE = sp.app_dir()
VERSION = "v1.6.5"
LAST_SCREEN = os.path.join(HERE, "last_screen.txt")
LAST_GRID = os.path.join(HERE, "last_grid.txt")
PLAYLIST = os.path.join(HERE, "playlist.txt")
SHOW_FLAG = os.path.join(HERE, "show_main.flag")
LAST_OFFSET = os.path.join(HERE, "last_offset.txt")
LAST_STRETCH = os.path.join(HERE, "last_stretch.txt")
BOOT_LOG = os.path.join(HERE, "boot.log")
VIDEO_TYPES = [("视频文件", "*.mp4 *.mov *.mkv *.avi *.wmv *.m4v *.mpg *.mpeg"),
               ("所有文件", "*.*")]


def read_last_screen():
    try:
        with open(LAST_SCREEN, "r", encoding="utf-8") as f:
            n = int(f.read().strip())
        return n
    except (OSError, ValueError):
        return None


def save_last_screen(n):
    try:
        with open(LAST_SCREEN, "w", encoding="utf-8") as f:
            f.write(str(n))
    except OSError:
        pass


def read_last_grid():
    """读取上次保存的时点间隔（秒）；没有或非法返回 None。"""
    try:
        with open(LAST_GRID, "r", encoding="utf-8") as f:
            interval = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if 1 <= interval <= 60:
        return [s for s in range(0, 60, interval)]
    return None


def save_last_grid(seconds):
    """保存当前时点间隔（秒），下次打开沿用。"""
    interval = (seconds[1] - seconds[0]) if len(seconds) >= 2 else 60
    try:
        with open(LAST_GRID, "w", encoding="utf-8") as f:
            f.write(str(interval))
    except OSError:
        pass


def read_last_offset():
    """读取上次保存的补偿（毫秒）；没有或非法返回 None。"""
    try:
        with open(LAST_OFFSET, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def save_last_offset(value):
    try:
        with open(LAST_OFFSET, "w", encoding="utf-8") as f:
            f.write("%d" % int(round(value)))
    except OSError:
        pass


def read_last_stretch():
    """读取上次是否勾选了「拉伸全屏」。"""
    try:
        with open(LAST_STRETCH, "r", encoding="utf-8") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def save_last_stretch(enabled):
    try:
        with open(LAST_STRETCH, "w", encoding="utf-8") as f:
            f.write("1" if enabled else "0")
    except OSError:
        pass


def boot_note(msg):
    try:
        with open(BOOT_LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


_MUTEX = None


def single_instance(role):
    """同一角色只允许开一个窗口；重复双击就把已有窗口提到前面。"""
    global _MUTEX
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\VJSyncer_" + role)
    if not _MUTEX:
        return True
    if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
        # 通知已在运行的实例把主界面叫出来（精简模式下它是隐藏的）
        try:
            with open(SHOW_FLAG, "w", encoding="utf-8") as f:
                f.write(role)
        except OSError:
            pass
        hwnd = sp.find_window("VJ Syncer")
        if hwnd:
            sp.force_foreground(hwnd)
        return False
    return True



def read_playlist():
    """读取播放列表；只保留仍然存在的文件。"""
    try:
        with open(PLAYLIST, "r", encoding="utf-8") as f:
            return [line.strip() for line in f
                    if line.strip() and os.path.isfile(line.strip())]
    except OSError:
        return []


def save_playlist(paths):
    try:
        with open(PLAYLIST, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(p + "\n")
    except OSError:
        pass


def enable_dpi():
    """按物理像素渲染，避免界面被系统拉伸发虚。

    打包版由 exe 的 manifest 声明 PerMonitorV2；这里主要给源码运行用。
    进程已经声明过的话这些调用会失败，忽略即可。
    """
    if sys.platform != "win32":
        return
    try:                                  # Win10 1703+：Per-Monitor V2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
            return
    except Exception:
        pass
    try:                                  # Win8.1+：Per-Monitor
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:                                  # 老系统：System
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def dpi_note(root):
    """把 DPI 感知级别和缩放写进 boot.log，方便排查清晰度问题。"""
    try:
        v = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v))
        boot_note("dpi aware=%d scaling=%.2f screen=%dx%d"
                  % (v.value, float(root.tk.call("tk", "scaling")),
                     root.winfo_screenwidth(), root.winfo_screenheight()))
    except Exception:
        pass


def fmt_cd(sec):
    if sec < 0:
        sec = 0.0
    m = int(sec // 60)
    s = sec - m * 60
    return "%02d:%04.1f" % (m, s)


def shorten(text, n):
    return text if len(text) <= n else "…" + text[-(n - 1):]


def pick_family(root, names):
    available = set(tkfont.families(root))
    for n in names:
        if n in available:
            return n
    return names[-1]


def monitor_count():
    try:
        return max(1, ctypes.windll.user32.GetSystemMetrics(80))  # SM_CMONITORS
    except Exception:
        return 1


def key_down(vk):
    """全局查询某个键是否按下（不看窗口焦点，Windows 专用）。"""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


class App(object):
    def __init__(self, root, cfg, topmost=True, compact=False):
        self.root = root
        self.cfg = cfg
        self.role = cfg.role
        self.seconds = sp.parse_grid(cfg.grid)
        self.topmost = topmost
        self.compact = compact    # 精简模式：不显示主界面，只显示右下角时点提示

        self.player = None
        self.midi = None
        self.fl_hwnd = None
        self.playlist = []
        self.playlist_index = 0
        self.video_ready = (self.role not in ("video", "both")) or self.cfg.dry_run
        self.has_fired = False
        self.overlay = None
        self.overlay_visible = False
        self.idle_hint = None
        self._hint_last = None
        self._hint_icon = None
        self._prev_enter = False
        self._prev_esc = False
        self._ui_modal = 0
        self._playlist_win = None
        self._closing = False
        self._starting = False
        self._arm_pending = False
        self._show_check = 0.0
        self._offset_mtime = 0.0
        self._offset_check = 0.0

        self.state = "loading"
        self.target = None
        self.eff = None
        self.next1 = None
        self.next2 = None
        self.focused = False
        self.clock_offset = None
        self.clock_cached = False        # 用的是上次保存的偏差（离线）
        self.clock_cached_ts = 0.0
        self._probe_busy = False
        self._probe_manual = False
        self.probe_q = queue.Queue()

        self.ui = pick_family(root, ["Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "Segoe UI"])
        self.mono = pick_family(root, ["Consolas", "Cascadia Mono", "Courier New"])

        self._build()
        self._bind()

    # ---------------------------------------------------------------- 界面

    def _role_text(self):
        return {"fl": "FL 端", "video": "视频端", "both": "FL + 视频", "none": "演练"}.get(self.role, self.role)

    def _build(self):
        root = self.root
        root.title("VJ Syncer — 双机同步起播  " + VERSION)
        root.configure(bg=BG)
        root.attributes("-topmost", self.topmost)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        head = tk.Frame(root, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 0))

        left = tk.Frame(head, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="VJ Syncer", font=(self.ui, 19, "bold"), fg=TEXT, bg=BG).pack(side="left")
        badge_text = self._role_text()
        if self.cfg.dry_run and self.role != "none":
            badge_text += " · 演练"
        tk.Label(left, text=badge_text, font=(self.ui, 9, "bold"), fg=ACCENT,
                 bg="#12222E", padx=9, pady=3).pack(side="left", padx=(10, 0))

        self.clock = tk.Label(head, text="", font=(self.mono, 11), fg=MUTED, bg=BG)
        self.clock.pack(side="right")

        self.reprobe_btn = tk.Button(
            head, text="重测时钟", command=lambda: self._probe_clock(manual=True),
            font=(self.ui, 8), bg=PANEL, fg=MUTED,
            activebackground="#202833", activeforeground=TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            padx=8, pady=2, cursor="hand2")
        self.reprobe_btn.pack(side="right", padx=(0, 10))

        gridf = tk.Frame(head, bg=BG)
        gridf.pack(side="right", padx=(0, 12))
        tk.Label(gridf, text="间隔", font=(self.ui, 9), fg=MUTED,
                 bg=BG).pack(side="left")
        self.grid_var = tk.StringVar(value=str(self._interval()))
        self.grid_entry = tk.Entry(
            gridf, textvariable=self.grid_var, width=3, justify="center",
            font=(self.mono, 10), bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER)
        self.grid_entry.pack(side="left", padx=(6, 3), ipady=2)
        self.grid_entry.bind("<Return>", self.apply_grid_entry)
        self.grid_entry.bind("<KP_Enter>", self.apply_grid_entry)
        self.grid_entry.bind("<FocusOut>", self.apply_grid_entry)
        tk.Label(gridf, text="秒", font=(self.ui, 9), fg=MUTED,
                 bg=BG).pack(side="left")

        self.grid_btn = tk.Menubutton(
            gridf, text="▾", font=(self.ui, 9, "bold"), bg=PANEL, fg=TEXT,
            activebackground="#202833", activeforeground=TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            padx=6, pady=2, cursor="hand2")
        grid_menu = tk.Menu(self.grid_btn, tearoff=0, bg=PANEL, fg=TEXT,
                            activebackground=ACCENT, activeforeground=DARK_TEXT,
                            font=(self.ui, 9), bd=0)
        for secs, label in GRID_CHOICES:
            grid_menu.add_command(label=label, command=lambda s=secs: self.set_grid(s))
        self.grid_btn["menu"] = grid_menu
        self.grid_btn.pack(side="left", padx=(4, 0))

        off = tk.Frame(head, bg=BG)
        off.pack(side="right", padx=(0, 14))
        tk.Label(off, text="补偿", font=(self.ui, 9), fg=MUTED, bg=BG).pack(side="left")
        self.offset_var = tk.StringVar(value="%d" % int(round(self.cfg.offset_ms)))
        self.offset_entry = tk.Entry(
            off, textvariable=self.offset_var, width=6, justify="center",
            font=(self.mono, 10), bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat", highlightthickness=1, highlightbackground=BORDER)
        self.offset_entry.pack(side="left", padx=(6, 4), ipady=2)
        tk.Label(off, text="ms", font=(self.ui, 9), fg=MUTED, bg=BG).pack(side="left")
        self.offset_entry.bind("<Return>", self.apply_offset)
        self.offset_entry.bind("<KP_Enter>", self.apply_offset)
        self.offset_entry.bind("<FocusOut>", self.apply_offset)

        self.status = tk.Label(root, text="●  加载中", font=(self.ui, 10, "bold"), fg=DIM, bg=BG)
        self.status.grid(row=1, column=0, sticky="w", padx=24, pady=(6, 0))

        mid = tk.Frame(root, bg=BG)
        mid.grid(row=2, column=0, sticky="nsew")
        box = tk.Frame(mid, bg=BG)
        box.pack(expand=True)
        self.cd = tk.Label(box, text="00:00.0", font=(self.mono, 72, "bold"), fg=DIM, bg=BG)
        self.cd.pack()
        tk.Label(box, text="倒计时", font=(self.ui, 9), fg=DIM, bg=BG).pack()

        if self.role in ("video", "both"):
            self._build_video_bar(root)

        wrap = tk.Frame(root, bg=BG)
        wrap.grid(row=4, column=0, sticky="ew", padx=24)
        wrap.columnconfigure(0, weight=1, uniform="c")
        wrap.columnconfigure(1, weight=1, uniform="c")
        self.c1 = self._card(wrap, "下一时点")
        self.c2 = self._card(wrap, "下下个时点")
        self.c1["frame"].grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.c2["frame"].grid(row=0, column=1, sticky="nsew", padx=(7, 0))

        self.msg = tk.Label(root, text="", font=(self.ui, 9), fg=MUTED, bg=BG, anchor="w")
        self.msg.grid(row=5, column=0, sticky="ew", padx=24, pady=(10, 0))

        bar = tk.Frame(root, bg=BG)
        bar.grid(row=6, column=0, sticky="ew", padx=24, pady=(10, 0))
        self.btn_arm = tk.Button(
            bar, text="就位  Enter", command=self.arm,
            font=(self.ui, 10, "bold"), bg=ACCENT, fg=DARK_TEXT,
            activebackground="#7FD4FF", activeforeground=DARK_TEXT,
            relief="flat", bd=0, highlightthickness=0, padx=26, pady=9, cursor="hand2")
        self.btn_arm.pack(side="left")
        self.btn_cancel = tk.Button(
            bar, text="取消  Esc", command=self.cancel,
            font=(self.ui, 10, "bold"), bg=PANEL, fg=TEXT,
            activebackground="#202833", activeforeground=TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            padx=26, pady=9, cursor="hand2")
        self.btn_cancel.pack(side="left", padx=10)
        self.btn_cancel.config(state="disabled", fg=DIM)

        if self.role in ("video", "both"):
            self.btn_end = tk.Button(
                bar, text="结束播放", command=self.end_playback,
                font=(self.ui, 10, "bold"), bg=PANEL, fg=RED,
                activebackground="#3A2226", activeforeground=RED,
                relief="flat", bd=0, highlightthickness=1,
                highlightbackground="#5A2A2A",
                padx=20, pady=9, cursor="hand2")
            self.btn_end.pack(side="right")

        self.foot = tk.Label(root, text="", font=(self.ui, 8), fg=DIM, bg=BG, anchor="w")
        self.foot.grid(row=7, column=0, sticky="ew", padx=24, pady=(8, 14))
        self.root.bind("<Map>", self._on_root_map)

    def _build_video_bar(self, root):
        bar = tk.Frame(root, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", padx=24, pady=(8, 8))
        bar.columnconfigure(0, weight=1)

        left = tk.Frame(bar, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        self.video_name = tk.Label(left, text="尚未添加视频", font=(self.ui, 11, "bold"),
                                   fg=TEXT, bg=BG, anchor="w")
        self.video_name.pack(fill="x")
        self.video_dir = tk.Label(left, text="点右边“播放列表…”添加", font=(self.ui, 8),
                                  fg=DIM, bg=BG, anchor="w")
        self.video_dir.pack(fill="x")

        right = tk.Frame(bar, bg=BG)
        right.grid(row=0, column=1, sticky="e", padx=(12, 0))

        self.screen_btn = tk.Menubutton(
            right, text=self._screen_label(self.cfg.screen) + "  ▾",
            font=(self.ui, 9, "bold"), bg=PANEL, fg=TEXT,
            activebackground="#202833", activeforeground=TEXT,
            relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
            padx=12, pady=6, cursor="hand2")
        menu = tk.Menu(self.screen_btn, tearoff=0, bg=PANEL, fg=TEXT,
                       activebackground=ACCENT, activeforeground=DARK_TEXT,
                       font=(self.ui, 9), bd=0)
        for i in range(monitor_count()):
            menu.add_command(label=self._screen_label(i),
                             command=lambda i=i: self.set_screen(i))
        self.screen_btn["menu"] = menu
        self.screen_btn.pack(side="left", padx=(0, 8))

        tk.Button(right, text="播放列表…", command=self.open_playlist_editor,
                  font=(self.ui, 9, "bold"), bg=PANEL, fg=TEXT,
                  activebackground="#202833", activeforeground=TEXT,
                  relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
                  padx=14, pady=6, cursor="hand2").pack(side="left", padx=(8, 0))

        self.stretch_var = tk.BooleanVar(
            value=bool(getattr(self.cfg, "stretch", False)))
        self.stretch_chk = tk.Checkbutton(
            right, text="拉伸全屏", variable=self.stretch_var,
            command=self.on_stretch_toggle,
            font=(self.ui, 9, "bold"), bg=BG, fg=TEXT,
            activebackground=BG, activeforeground=TEXT,
            selectcolor=PANEL, relief="flat", bd=0,
            highlightthickness=0, padx=6, pady=4, cursor="hand2")
        self.stretch_chk.pack(side="left", padx=(10, 0))

    def _screen_label(self, index):
        return ("屏幕 %d（主屏）" % index) if index == 0 else ("屏幕 %d" % index)

    def set_screen(self, index):
        if self.state != "idle":
            self.set_msg("先按 Esc 回到待就位，再切屏幕", AMBER)
            return
        if index == self.cfg.screen:
            return
        self.cfg.screen = index
        save_last_screen(index)
        self.screen_btn.config(text=self._screen_label(index) + "  ▾")

        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass
            self.player = None
        self.video_ready = False

        if self.role in ("video", "both") and not self.cfg.dry_run and self.cfg.player == "mpv":
            try:
                self.player = sp.make_player(self.cfg, idle=True)
            except SystemExit as e:
                self.set_status("视频未就绪", RED)
                self.set_msg("播放器没起来，稍后重试或重启程序", AMBER)
                self.refresh_buttons()
                messagebox.showerror("播放器启动失败", str(e), parent=self.root)
                return

        if self.playlist and not self.cfg.dry_run and self.player is not None:
            item = self.playlist[min(self.playlist_index, len(self.playlist) - 1)]
            try:
                self.player.load(item)
                self.video_ready = True
                self._update_playlist_display()
            except Exception:
                self.video_ready = False
        if not self.playlist:
            self.set_status("请添加视频", AMBER)
            self.set_msg("点“播放列表…”把要播的视频加进去", MUTED)
        else:
            self.set_msg("输出屏幕已切到 %d" % index, MUTED)
        self.refresh_buttons()

    def _interval(self):
        secs = self.seconds
        return (secs[1] - secs[0]) if len(secs) >= 2 else 60

    def set_grid(self, seconds):
        if self.state != "idle":
            self.set_msg("先按 Esc 回到待就位，再改间隔", AMBER)
            return
        self.seconds = list(seconds)
        save_last_grid(self.seconds)
        interval = self._interval()
        self.grid_var.set(str(interval))
        self.update_footer()
        self.update_idle(time.time())
        self.set_msg("间隔已改为 %d 秒，时点 %s"
                     % (interval, "/".join(str(s) for s in self.seconds)), MUTED)

    def apply_grid_entry(self, _event=None):
        """直接在主界面输入间隔（2 ~ 60 秒），不弹窗。"""
        text = self.grid_var.get().strip()
        cur = str(self._interval())
        if text == cur:
            return "break"
        try:
            value = int(float(text))
        except (ValueError, tk.TclError):
            self.grid_var.set(cur)
            self.set_msg("间隔要填 2~60 的整数（秒）", AMBER)
            return "break"
        value = max(2, min(60, value))
        if self.state != "idle":
            self.grid_var.set(cur)
            self.set_msg("先按 Esc 回到待就位，再改间隔", AMBER)
            return "break"
        self.set_grid([s for s in range(0, 60, value)])
        if 60 % value:
            self.set_msg("间隔 %d 秒（提示：%d 不能整除 60，跨分钟处会有一个短间隔）"
                         % (value, value), AMBER)
        self.root.focus_set()          # 提交后收起光标
        return "break"

    def apply_offset(self, _event=None):
        """应用界面上的补偿（毫秒，正=延后，负=提前）。"""
        try:
            value = float(self.offset_var.get().strip())
        except (ValueError, tk.TclError):
            self.offset_var.set("%d" % int(round(self.cfg.offset_ms)))
            self.set_msg("补偿要填数字（毫秒），例如 -80", AMBER)
            return "break"
        value = max(-2000.0, min(2000.0, round(value)))
        if self.state != "idle":
            self.offset_var.set("%d" % int(round(self.cfg.offset_ms)))
            self.set_msg("先按 Esc 回到待就位，再改补偿", AMBER)
            self.root.focus_set()
            return "break"
        if abs(value - self.cfg.offset_ms) < 0.5:
            self.root.focus_set()      # 值没变，只收起光标
            return "break"
        self.cfg.offset_ms = value
        save_last_offset(value)
        self.update_footer()
        self.set_msg("补偿已设为 %+d ms（负数 = 提前触发）" % int(value), MUTED)
        self.root.focus_set()
        return "break"

    # ---------------------------------------------------------------- 播放列表

    def _current_item(self):
        if self.playlist and self.playlist_index < len(self.playlist):
            return self.playlist[self.playlist_index]
        return ""

    def _playlist_done(self):
        return bool(self.playlist) and self.playlist_index >= len(self.playlist)

    def _update_playlist_display(self):
        if not hasattr(self, "video_name"):
            return
        item = self._current_item()
        if self.playlist and item:
            self.video_name.config(text=shorten(os.path.basename(item), 42))
            self.video_dir.config(text="第 %d/%d 项 · 播放列表"
                                  % (self.playlist_index + 1, len(self.playlist)))
        elif self._playlist_done():
            self.video_name.config(text="已播完")
            self.video_dir.config(text="播放列表 %d 项" % len(self.playlist))
        elif not self.playlist:
            self.video_name.config(text="尚未添加视频")
            self.video_dir.config(text="点“播放列表…”添加要播的视频")

    def _prepare_playlist(self):
        """列表变化后：把第一段重新载入播放器并置为就绪（就位按钮才会亮）。"""
        if not self.playlist:
            self.video_ready = False
            if self.state == "idle":
                self.set_status("请添加视频", AMBER)
                self.set_msg("点“播放列表…”把要播的视频加进去", MUTED)
            return
        self.video_ready = bool(self.cfg.dry_run or self.player is not None)
        if self.video_ready and not self.cfg.dry_run and self.player is not None:
            try:
                self.player.load(self.playlist[0])
            except Exception:
                pass
        if self.state == "idle":
            if self.video_ready:
                self.set_status("待就位", DIM)
                self.set_msg("播放列表 %d 项 · 第 1 项已就绪 · Enter 启动"
                             % len(self.playlist), MUTED)
            else:
                self.set_status("视频未就绪", RED)
                self.set_msg("播放器没起来，稍后重试或重启程序", AMBER)

    def open_playlist_editor(self):
        if self.state != "idle":
            self.set_msg("先复位回到待就位，再编辑列表", AMBER)
            return
        win = tk.Toplevel(self.root)
        win.title("播放列表")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.geometry("540x380")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)
        self._playlist_win = win
        win.bind("<Destroy>", lambda e, w=win: self._clear_playlist_win(e, w))

        box = tk.Frame(win, bg=BG)
        box.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        lb = tk.Listbox(box, bg=CARD, fg=TEXT, selectbackground=ACCENT,
                        selectforeground=DARK_TEXT, font=(self.ui, 10),
                        activestyle="none", relief="flat", highlightthickness=1,
                        highlightbackground=BORDER)
        lb.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(box, orient="vertical", command=lb.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        lb.config(yscrollcommand=scroll.set)

        btns = tk.Frame(box, bg=BG)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        def refresh(sel=None):
            lb.delete(0, "end")
            for i, p in enumerate(self.playlist):
                lb.insert("end", "%d. %s" % (i + 1, os.path.basename(p)))
            if sel is not None:
                lb.selection_set(sel)

        def commit(sel=None):
            self.playlist_index = 0
            save_playlist(self.playlist)
            self._prepare_playlist()
            self._update_playlist_display()
            self.refresh_buttons()
            refresh(sel)

        def add():
            paths = self._dialog(
                filedialog.askopenfilenames,
                title="添加视频到播放列表", initialdir=HERE, parent=win,
                filetypes=VIDEO_TYPES)
            if not paths:
                return
            changed = False
            for p in paths:
                p = os.path.normpath(p)
                if p and p not in self.playlist:
                    self.playlist.append(p)
                    changed = True
            if changed:
                commit()

        def remove():
            sel = lb.curselection()
            if sel:
                del self.playlist[sel[0]]
                commit()

        def move(delta):
            sel = lb.curselection()
            if not sel:
                return
            i = sel[0]
            j = i + delta
            if 0 <= j < len(self.playlist):
                self.playlist[i], self.playlist[j] = self.playlist[j], self.playlist[i]
                commit(j)

        def up():
            move(-1)

        def down():
            move(1)

        def clear():
            if self._dialog(messagebox.askyesno, "清空列表",
                            "确定清空整个播放列表？", parent=win):
                self.playlist = []
                commit()

        for text, cmd in (("添加…", add), ("上移", up), ("下移", down),
                          ("删除", remove), ("清空", clear)):
            tk.Button(btns, text=text, command=cmd,
                      font=(self.ui, 9, "bold"), bg=PANEL, fg=TEXT,
                      activebackground="#202833", activeforeground=TEXT,
                      relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER,
                      padx=12, pady=5, cursor="hand2").pack(side="left", padx=(0, 8))

        refresh()
        win.lift()
        win.focus_force()

    def on_stretch_toggle(self):
        """勾选/取消「拉伸全屏」：立即生效并记住。"""
        val = bool(self.stretch_var.get())
        self.cfg.stretch = val
        save_last_stretch(val)
        if self.player is not None:
            try:
                self.player.set_keepaspect(not val)   # keep=False 即拉伸
            except Exception:
                pass
        self.set_msg("拉伸全屏：%s" % ("开" if val else "关"), MUTED)

    def load_video(self, path):
        if not os.path.isfile(path):
            messagebox.showerror("文件不存在", path, parent=self.root)
            return False

        self.cfg.file = path
        if self.video_name is not None:
            self.video_name.config(text=shorten(os.path.basename(path), 42))
            self.video_dir.config(text=shorten(os.path.dirname(path), 62))

        self.set_status("加载视频…", AMBER)
        self.set_msg("正在加载 " + os.path.basename(path), MUTED)
        self.refresh_buttons()
        self.root.update_idletasks()

        if self.cfg.dry_run:
            self.video_ready = True
            self._video_ready_ui(path, "演练模式：不加载播放器")
            return True

        # 已经有一个开着的 mpv：直接换片，不再重启进程
        if self.player is not None and hasattr(self.player, "load"):
            try:
                self.player.load(path)
            except Exception:
                self.player = None
            else:
                self.video_ready = True
                self._video_ready_ui(path)
                return True

        if self.player is not None:
            try:
                self.player.stop()
            except Exception:
                pass
            self.player = None

        try:
            self.player = sp.make_player(self.cfg)
        except SystemExit as e:
            self.player = None
            self.video_ready = False
            self.set_status("视频未就绪", RED)
            self.set_msg("加载失败，检查一下播放器", AMBER)
            self.refresh_buttons()
            messagebox.showerror("视频加载失败", str(e), parent=self.root)
            return False
        except Exception as e:
            self.player = None
            self.video_ready = False
            self.set_status("视频未就绪", RED)
            self.set_msg("加载失败：" + str(e), RED)
            self.refresh_buttons()
            return False

        self.video_ready = True
        self._video_ready_ui(path)
        return True

    def _video_ready_ui(self, path, note=None):
        if self.state == "idle":
            self.set_status("待就位", DIM)
            self.set_msg(note or "Enter 就位 · Esc 取消", MUTED)
        if getattr(self, "screen_btn", None) is not None:
            self.screen_btn.config(text=self._screen_label(self.cfg.screen) + "  ▾")
        self.refresh_buttons()
        self.update_footer()
        self.root.after(120, self.bring_front)
        self.root.after(700, self.bring_front)

    def _card(self, parent, title):
        frame = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        inner = tk.Frame(frame, bg=CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=11)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=title, font=(self.ui, 9), fg=MUTED, bg=CARD).pack(side="left")
        badge = tk.Label(top, text="目标", font=(self.ui, 8, "bold"), fg=DARK_TEXT, bg=ACCENT,
                         padx=7, pady=1)

        tm_row = tk.Frame(inner, bg=CARD)
        tm_row.pack(fill="x", pady=(5, 0))
        tm_head = tk.Label(tm_row, text="--", font=(self.mono, 22, "bold"), fg=TEXT,
                           bg=CARD, padx=0, pady=0, bd=0)
        tm_head.pack(side="left")
        tm_tail = tk.Label(tm_row, text=":--:--", font=(self.mono, 22, "bold"), fg=TEXT,
                           bg=CARD, padx=0, pady=0, bd=0)
        tm_tail.pack(side="left")
        sub = tk.Label(inner, text="", font=(self.ui, 9), fg=DIM, bg=CARD, anchor="w")
        sub.pack(fill="x")
        return {"frame": frame, "badge": badge,
                "t_head": tm_head, "t_tail": tm_tail, "sub": sub}

    def _bind(self):
        self.root.bind("<Return>", self._on_enter)
        self.root.bind("<KP_Enter>", self._on_enter)
        self.root.bind("<Escape>", self._on_esc)
        self.root.bind("<Button-1>", self._drop_entry_focus, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.focus_force()

    def _drop_entry_focus(self, event=None):
        """点了输入框以外的地方 -> 收起输入焦点，光标不再闪。"""
        w = getattr(event, "widget", None)
        if isinstance(w, tk.Entry):
            return                       # 点在输入框上：正常聚焦它
        try:
            focus = self.root.focus_get()
        except Exception:
            return
        if isinstance(focus, tk.Entry):
            try:
                self.root.focus_set()
            except Exception:
                pass

    def _on_enter(self, _event=None):
        self.arm()
        return "break"

    def _on_esc(self, _event=None):
        self._handle_esc()
        return "break"

    def _on_close(self):
        self._closing = True
        if self.player is not None and not self.has_fired:
            try:
                self.player.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---------------------------------------------------------------- 状态

    def set_status(self, text, color):
        self.status.config(text="●  " + text, fg=color)

    def set_msg(self, text, color=MUTED):
        self.msg.config(text=text, fg=color)

    def _clock_correction(self):
        """自动补偿系统时钟与 NTP 的偏差（秒）。

        clock_offset = 服务器 - 本机；本机落后时为正，需要提前触发同样的量。
        只在本机偏差明显（> 40ms）时才补：NTP 探测本身有网络不对称误差，
        偏差很小时补进去反而引入噪声。
        """
        off = self.clock_offset
        if off is None or abs(off) <= 40.0 or abs(off) > 2000.0:
            return 0.0
        return -off / 1000.0

    def update_footer(self):
        parts = []
        if not self.cfg.no_ntp:
            if self.clock_offset is None:
                parts.append("时钟 检测中…")
            else:
                corr = self._clock_correction() * 1000.0
                if self.clock_cached:
                    when = ""
                    if self.clock_cached_ts:
                        when = time.strftime(
                            "%m-%d", time.localtime(self.clock_cached_ts))
                    parts.append(
                        "时钟 %+.0f ms（离线缓存%s%s）"
                        % (self.clock_offset,
                           " " + when if when else "",
                           " · 已补偿" if corr else ""))
                elif corr:
                    parts.append("时钟 %+.0f ms（已自动补偿 %+.0f ms）"
                                 % (self.clock_offset, corr))
                else:
                    parts.append("时钟 %+.0f ms" % self.clock_offset)
        parts.append("补偿 %+.0f ms" % self.cfg.offset_ms)
        if self.role in ("video", "both"):
            if self.video_ready:
                parts.append("视频 已就绪")
            elif self.player is not None:
                parts.append("播放器 待命")
            else:
                parts.append("播放器 未启动")
        if self.playlist:
            parts.append("列表 %d/%d"
                         % (min(self.playlist_index + 1, len(self.playlist)), len(self.playlist)))
        if self.cfg.fl_midi_port:
            parts.append("MIDI " + self.cfg.fl_midi_port)
        parts.append("时间表 " + "/".join(str(s) for s in self.seconds) + " 秒")
        parts.append(VERSION)
        self.foot.config(text="   ·   ".join(parts))

    def _clear_target(self):
        for c in (self.c1, self.c2):
            c["frame"].config(highlightbackground=BORDER)
            c["badge"].pack_forget()

    def _mark_target(self, card):
        self._clear_target()
        card["frame"].config(highlightbackground=ACCENT)
        card["badge"].pack(side="right")

    def _set_card(self, card, ts, now, highlight=False):
        """ts 是实际会使用的时点；副标题显示距它作废还剩多少秒。"""
        hms = time.strftime("%H:%M:%S", time.localtime(ts))
        card["t_head"].config(text=hms[:3])
        card["t_tail"].config(text=hms[3:], fg=(RED if highlight else TEXT))
        left = max(0.0, ts - GUI_LEAD - now)
        card["sub"].config(text="%.1f 秒内可用" % left,
                           fg=(RED if highlight else DIM))

    def refresh_buttons(self):
        idle = self.state == "idle"
        has_video = self.video_ready and not self._playlist_done()
        if idle and has_video:
            self.btn_arm.config(state="normal", bg=ACCENT, fg=DARK_TEXT)
        else:
            self.btn_arm.config(state="disabled", bg="#22303C", fg="#6B7A8A")
        if self.state in ("armed", "prepare", "fired", "error"):
            self.btn_cancel.config(state="normal", bg=PANEL, fg=TEXT)
        else:
            self.btn_cancel.config(state="disabled", bg=PANEL, fg=DIM)

    # ---------------------------------------------------------------- 启动

    def start(self):
        boot_note("app start")
        boot_note("version " + VERSION)
        dpi_note(self.root)
        try:
            os.remove(SHOW_FLAG)          # 清掉上次遗留的"显示主界面"请求
        except OSError:
            pass
        try:
            self._offset_mtime = os.path.getmtime(LAST_OFFSET)
        except OSError:
            self._offset_mtime = 0.0
        self.root.update_idletasks()
        self.state = "idle"
        self._starting = True
        self._key_loop()        # 尽早开始监听：启动途中按的 Enter 也不会丢

        if self.role in ("fl", "both") and self.cfg.fl_midi_port:
            self.set_status("连接 MIDI…", AMBER)
            self.root.update_idletasks()
            try:
                self.midi = sp.MidiTrigger(self.cfg.fl_midi_port,
                                           self.cfg.fl_midi_note, self.cfg.fl_midi_channel)
            except SystemExit as e:
                messagebox.showerror("MIDI 连接失败", str(e), parent=self.root)

        if self.role in ("video", "both"):
            if not self.cfg.dry_run and self.cfg.player == "mpv":
                self.set_status("启动播放器…", AMBER)
                self.root.update_idletasks()
                try:
                    self.player = sp.make_player(self.cfg, idle=True)
                    boot_note("mpv idle ready")
                except SystemExit as e:
                    self.player = None
                    self.video_ready = False
                    self.set_status("视频未就绪", RED)
                    self.set_msg("播放器没起来，稍后重试或重启程序", AMBER)
                    messagebox.showerror("播放器启动失败", str(e), parent=self.root)
                except Exception as e:
                    self.player = None
                    self.video_ready = False
                    self.set_status("视频未就绪", RED)
                    self.set_msg("播放器没起来：" + str(e), RED)

            self.playlist = read_playlist()
            if not self.playlist and self.cfg.file and os.path.isfile(self.cfg.file):
                self.playlist = [self.cfg.file]      # 命令行 --file 当成单项列表
            self.playlist_index = 0
            if self.playlist:
                self.video_ready = True
                if not self.cfg.dry_run and self.player is not None:
                    try:
                        self.player.load(self.playlist[0])   # 打开时把第一段重置到开头
                    except Exception:
                        pass
                self._update_playlist_display()
                self.set_status("待就位", DIM)
                self.set_msg("播放列表 %d 项 · 第 1 项已就绪 · Enter 启动"
                             % len(self.playlist), MUTED)
            else:
                self.set_status("请添加视频", AMBER)
                self.set_msg("点“播放列表…”把要播的视频加进去", MUTED)
        else:
            self.set_status("待就位", DIM)
            self.set_msg("Enter 就位 · Esc 取消", MUTED)

        self.refresh_buttons()
        self.update_footer()
        self._probe_clock()
        self.root.after(PROBE_INTERVAL, self._probe_tick)
        self.heartbeat()
        self._starting = False
        boot_note("app ready")
        if self._arm_pending:
            self._arm_pending = False
            self.root.after(120, self.arm)   # 启动途中按下的 Enter，就绪后补上
        if self.compact:
            if self.playlist:
                boot_note("compact mode: main window hidden")
            else:
                boot_note("compact mode: empty playlist, show main window")
                self.root.deiconify()
        self.root.after(200, self.bring_front)
        self.root.after(600, self.log_window_state)
        self.root.after(2500, self.log_window_state)

    def bring_front(self):
        if self.state in ("armed", "prepare"):
            return
        if self.compact:
            return          # 精简模式：不自动弹出主界面
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def log_window_state(self):
        try:
            boot_note("window state=%s geom=%s mapped=%s topmost=%s"
                      % (self.root.state(), self.root.winfo_geometry(),
                         self.root.winfo_ismapped(), self.root.attributes("-topmost")))
        except Exception:
            pass

    # ---------------------------------------------------------------- 小浮窗

    def _ensure_overlay(self):
        if self.overlay is not None:
            return
        ov = tk.Toplevel(self.root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        try:
            ov.attributes("-alpha", 0.94)
        except Exception:
            pass
        ov.configure(bg=BG)

        frame = tk.Frame(ov, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill="both", expand=True)

        left = tk.Frame(frame, bg=PANEL)
        left.pack(side="left", padx=(13, 10), pady=9)
        ov_row = tk.Frame(left, bg=PANEL)
        ov_row.pack(anchor="w")
        self.ov_time_head = tk.Label(ov_row, text="--", font=(self.mono, 14, "bold"),
                                     fg=TEXT, bg=PANEL, padx=0, pady=0, bd=0)
        self.ov_time_head.pack(side="left")
        self.ov_time_tail = tk.Label(ov_row, text=":--:--", font=(self.mono, 14, "bold"),
                                     fg=RED, bg=PANEL, padx=0, pady=0, bd=0)
        self.ov_time_tail.pack(side="left")
        tk.Label(left, text="下一时点", font=(self.ui, 7), fg=DIM, bg=PANEL).pack(anchor="w")

        right = tk.Frame(frame, bg=PANEL)
        right.pack(side="right", padx=(10, 15), pady=9)
        self.ov_cd = tk.Label(right, text="00:00.0", font=(self.mono, 24, "bold"),
                              fg=ACCENT, bg=PANEL)
        self.ov_cd.pack(anchor="e")
        tk.Label(right, text="倒计时", font=(self.ui, 7), fg=DIM, bg=PANEL).pack(anchor="e")

        for widget in (ov, frame, left, right, ov_row,
                       self.ov_time_head, self.ov_time_tail, self.ov_cd):
            widget.bind("<Button-1>", lambda e: self._restore_from_overlay())
        ov.bind("<Escape>", lambda e: self._handle_esc())
        self.overlay = ov

    def _work_area(self):
        try:
            hwnd = sp.find_window("VJ Syncer")
            if hwnd:
                u = ctypes.windll.user32
                u.MonitorFromWindow.restype = ctypes.c_void_p
                u.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_ulong),
                                ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT),
                                ("dwFlags", ctypes.c_ulong)]

                mon = u.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
                if u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    r = mi.rcWork
                    return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def show_overlay(self):
        self._hide_idle_hint()
        self._ensure_overlay()
        hms = time.strftime("%H:%M:%S", time.localtime(self.target))
        self.ov_time_head.config(text=hms[:3])
        self.ov_time_tail.config(text=hms[3:])
        self.ov_cd.config(text=fmt_cd(max(0.0, self.eff - time.time())), fg=ACCENT)
        self.overlay_visible = True
        self.overlay.deiconify()
        self.overlay.update_idletasks()
        w = self.overlay.winfo_reqwidth()
        h = self.overlay.winfo_reqheight()
        left, top, right, _bottom = self._work_area()
        x = max(left, right - w - 16)
        y = top + 16
        self.overlay.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.overlay.lift()
        if self.role == "video":
            try:
                self.overlay.focus_force()
            except Exception:
                pass
        if not self.compact:
            self.root.iconify()

    def hide_overlay(self):
        if self.overlay is not None and self.overlay_visible:
            self.overlay.withdraw()
        self.overlay_visible = False

    # ---------------------------------------------------------------- 空档期提示

    def _ensure_idle_hint(self):
        if self.idle_hint is not None:
            return
        ov = tk.Toplevel(self.root)
        ov.withdraw()
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.configure(bg=HINT_KEY)
        try:
            ov.attributes("-transparentcolor", HINT_KEY)    # 背景透明（Windows）
        except Exception:
            pass
        frame = tk.Frame(ov, bg=HINT_KEY)
        frame.pack(padx=4, pady=2)
        self.hint_icon = tk.Canvas(frame, width=18, height=18, bg=HINT_KEY,
                                   highlightthickness=0, bd=0)
        self.hint_icon.pack(side="left", padx=(0, 7))
        col = tk.Frame(frame, bg=HINT_KEY)
        col.pack(side="left")
        row1 = tk.Frame(col, bg=HINT_KEY)
        row1.pack()
        self.hint_label = tk.Label(row1, text="", font=(self.mono, 11, "bold"),
                                   fg=TEXT, bg=HINT_KEY, padx=0, pady=0, bd=0)
        self.hint_label.pack(side="left")
        self.hint_tail = tk.Label(row1, text="", font=(self.mono, 11, "bold"),
                                  fg=RED, bg=HINT_KEY, padx=0, pady=0, bd=0)
        self.hint_tail.pack(side="left")
        row2 = tk.Frame(col, bg=HINT_KEY)
        row2.pack()
        self.hint_cd = tk.Label(row2, text="", font=(self.mono, 11, "bold"),
                                fg=GREEN, bg=HINT_KEY, padx=0, pady=0, bd=0)
        self.hint_cd.pack()
        for w in (ov, frame, col, row1, self.hint_icon, self.hint_label,
                  self.hint_tail, row2, self.hint_cd):
            w.bind("<Button-1>", lambda e: self._restore_from_overlay())
        ov.bind("<Return>", lambda e: self.arm())
        ov.bind("<KP_Enter>", lambda e: self.arm())
        ov.bind("<Escape>", lambda e: self._handle_esc())
        self.idle_hint = ov

    def _draw_hint_icon(self, kind, color):
        """右下角状态图标：play = 等待 Enter；clock = 倒计时中。"""
        c = self.hint_icon
        c.delete("all")
        if kind == "play":
            c.create_polygon(4.5, 2.8, 15.2, 9, 4.5, 15.2, fill=color, outline=color)
        else:
            c.create_oval(1, 1, 17, 17, outline=color, width=2)
            c.create_line(9, 9, 9, 4.6, fill=color, width=2)
            c.create_line(9, 9, 12.5, 9, fill=color, width=2)

    def _separate_screen(self):
        """视频输出到“另一块”屏幕（主界面所在屏幕之外）。

        双屏时主界面可以一直看着，不需要再隐藏它，也不往播放屏上叠右下角提示。
        """
        try:
            return monitor_count() > 1 and self.cfg.screen != 0
        except Exception:
            return False

    def _sync_hint(self):
        """右下角提示（两行）：上行时点（分:秒标红），下行倒计时秒数。"""
        if (self.role not in ("video", "both") or not self.playlist
                or self.state not in ("idle", "armed", "prepare")
                or self._playlist_done()):
            # 全部播完不再提示（此时按 Esc 直接退出）
            self._hide_idle_hint()
            return
        if not self.compact and self._separate_screen():
            # 双屏：主界面一直可见，右下角提示就不用了
            self._hide_idle_hint()
            return
        self._ensure_idle_hint()
        now = time.time()
        if self.state in ("armed", "prepare"):
            hms = time.strftime("%H:%M:%S", time.localtime(self.target))
            cd_text = "%.1f" % max(0.0, self.eff - now)
            cd_color = AMBER if self.state == "prepare" else GREEN
        else:
            slot = sp.next_slot(now, self.seconds, GUI_LEAD)
            hms = time.strftime("%H:%M:%S", time.localtime(slot))
            cd_text = "%.1f" % max(0.0, slot - GUI_LEAD - now)
            cd_color = GREEN
        head, tail = hms[:3], hms[3:]
        icon = ("clock" if self.state in ("armed", "prepare") else "play", cd_color)
        if icon != self._hint_icon:
            self._hint_icon = icon
            self._draw_hint_icon(*icon)
        text = head + tail + "|" + cd_text
        if text == self._hint_last:
            return
        prev = self._hint_last
        first_show = prev is None
        need_resize = first_show or len(text) != len(prev or "")
        self._hint_last = text
        self.hint_label.config(text=head)
        self.hint_tail.config(text=tail)
        self.hint_cd.config(text=cd_text, fg=cd_color)
        if need_resize:
            self.idle_hint.update_idletasks()
            w = self.idle_hint.winfo_reqwidth()
            h = self.idle_hint.winfo_reqheight()
            left, top, right, bottom = self._work_area()
            x = max(left, right - w - 12)
            y = bottom - h - 12
            self.idle_hint.geometry("%dx%d+%d+%d" % (w, h, x, y))
        if first_show:
            self.idle_hint.deiconify()
            self.idle_hint.lift()
            self.idle_hint.focus_force()

    def _hide_idle_hint(self):
        if self.idle_hint is not None:
            self.idle_hint.withdraw()
        self._hint_last = None

    def _restore_from_overlay(self):
        self.hide_overlay()
        if self.compact:
            # 精简模式下点一下提示：临时叫出主界面，方便配置/检查
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass
            return
        self.bring_front()

    def _on_root_map(self, event):
        if event.widget is self.root and self.overlay_visible:
            self.hide_overlay()

    def _probe_clock(self, manual=False):
        """测一次时钟偏差；manual=True 表示用户点「重测时钟」。"""
        if self.cfg.no_ntp:
            if manual:
                self.set_msg("启动时带了 --no-ntp，不做时钟检测", AMBER)
            self.update_footer()
            return
        if self._probe_busy:
            if manual:
                self.set_msg("正在测时钟，稍等…", MUTED)
            return
        self._probe_busy = True
        self._probe_manual = manual
        if manual:
            self.set_msg("正在重测时钟偏差…", MUTED)

        def work():
            best = None
            for _ in range(3):
                result = sp.ntp_probe(self.cfg.ntp_server)
                if result and (best is None or result[1] < best[1]):
                    best = result
            self.probe_q.put(best)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(200, self._poll_probe)

    def _probe_tick(self):
        """每 10 分钟自动重测一次（倒计时/演出中推迟）。"""
        if self._closing:
            return
        if self.state in ("armed", "prepare", "fired"):
            self.root.after(PROBE_RETRY, self._probe_tick)
            return
        self._probe_clock()
        self.root.after(PROBE_INTERVAL, self._probe_tick)

    def _poll_probe(self):
        try:
            result = self.probe_q.get_nowait()
        except queue.Empty:
            self.root.after(200, self._poll_probe)
            return
        manual = self._probe_manual
        self._probe_busy = False
        self._probe_manual = False
        if result:
            self.clock_offset = result[0] * 1000.0
            self.clock_cached = False
            sp.save_clock_cache(self.clock_offset, self.cfg.ntp_server)
            if manual:
                self.set_msg("时钟重测完成：%+.0f ms" % self.clock_offset, MUTED)
        else:
            # 没联网 / 探测失败：保持原来的值（首次启动时才退回上次保存的值）
            if self.clock_offset is None:
                cached = sp.read_clock_cache()
                if cached is not None:
                    self.clock_offset = cached[0]
                    self.clock_cached_ts = cached[1]
                    self.clock_cached = True
            if manual:
                if self.clock_offset is None:
                    self.set_msg("没联网，也没测到过，暂时按系统时钟", AMBER)
                else:
                    self.set_msg("没联网，继续用上次的 %+.0f ms" % self.clock_offset,
                                 AMBER)
        self.update_footer()

    # ---------------------------------------------------------------- 全局按键

    def _dialog(self, fn, *args, **kwargs):
        """弹窗期间暂停全局按键监听（避免在选文件时按 Enter 误触发就位）。"""
        self._ui_modal += 1
        try:
            return fn(*args, **kwargs)
        finally:
            self._ui_modal -= 1

    def _clear_playlist_win(self, event, win):
        if event.widget is win and self._playlist_win is win:
            self._playlist_win = None

    def _poll_global_keys(self):
        """不看焦点地监听 Enter / Esc（mpv 全屏时窗口往往拿不到焦点）。"""
        try:
            focus = self.root.focus_get()
        except Exception:
            focus = None
        typing = isinstance(focus, tk.Entry)
        if (self._ui_modal > 0 or self._playlist_win is not None or typing):
            # 弹窗/输入框期间只同步按键状态，不响应，避免误触发
            self._prev_enter = key_down(0x0D)
            self._prev_esc = key_down(0x1B)
            return
        down = key_down(0x0D)
        if down and not self._prev_enter:
            try:
                if self._starting or self.state == "loading":
                    self._arm_pending = True      # 启动途中按的 Enter，就绪后补上
                else:
                    self.arm()
            except Exception:
                pass
        self._prev_enter = down
        down = key_down(0x1B)
        if down and not self._prev_esc:
            try:
                self._handle_esc()
            except Exception:
                pass
        self._prev_esc = down

    def _key_loop(self):
        if self._closing:
            return
        try:
            self._poll_global_keys()
        except Exception:
            pass
        finally:
            if not self._closing:
                try:
                    self.root.after(25, self._key_loop)
                except Exception:
                    pass

    # ---------------------------------------------------------------- 主循环

    def _check_show_request(self):
        """另一个实例双击时留下的"显示主界面"请求（精简模式下尤其需要）。"""
        try:
            if not os.path.exists(SHOW_FLAG):
                return
            if self.state in ("armed", "prepare"):
                return                      # 倒计时中不打断，等它结束
            try:
                os.remove(SHOW_FLAG)
            except OSError:
                pass
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            boot_note("show main window (requested by another launch)")
            if self._playlist_done():
                # 已经播完：重新打开时重置到第一项，不要停在"已播完"
                self.playlist_index = 0
                if not self.cfg.dry_run and self.player is not None and self.playlist:
                    try:
                        self.player.load(self.playlist[0])
                    except Exception:
                        pass
                self._reset_to_idle(advance=False)
                boot_note("reset playlist to first item")
        except Exception:
            pass

    def _sync_offset_file(self):
        """对时工具改了 last_offset.txt 时，待就位状态下自动跟上。"""
        if "--offset-ms" in sys.argv:
            return                       # 命令行显式指定时以命令行为准
        try:
            mtime = os.path.getmtime(LAST_OFFSET)
        except OSError:
            return
        if mtime == self._offset_mtime:
            return
        self._offset_mtime = mtime
        value = read_last_offset()
        if value is None or abs(value - self.cfg.offset_ms) < 0.5:
            return
        if self.state != "idle":
            return
        self.cfg.offset_ms = value
        try:
            self.offset_var.set("%d" % int(round(value)))
        except Exception:
            pass
        self.update_footer()
        self.set_msg("补偿已同步为 %+d ms（来自对时工具）" % int(round(value)), MUTED)

    def heartbeat(self):
        now = time.time()
        if now - self._show_check > 0.3:
            self._show_check = now
            self._check_show_request()
        if now - self._offset_check > 1.0:
            self._offset_check = now
            self._sync_offset_file()
        self.clock.config(text=time.strftime("%H:%M:%S"))
        if self.state == "idle":
            self.update_idle(now)
            self._sync_hint()
            delay = 100
        elif self.state in ("armed", "prepare"):
            self.update_armed(now)
            self._sync_hint()
            delay = 10 if (self.eff - now) < 3 else 20
        elif self.state == "fired":
            self.update_fired(now)
            delay = 200
        else:
            delay = 200
        if not self._closing:
            self.root.after(delay, self.heartbeat)

    def update_idle(self, now):
        # 直接取"会被使用的那个时点"：距现在不足 lead 的格子已被 next_slot 跳过
        t1 = sp.next_slot(now, self.seconds, GUI_LEAD)
        t2 = sp.next_slot(t1, self.seconds, 0.001)
        self.next1, self.next2 = t1, t2
        self._set_card(self.c1, t1, now, highlight=True)
        self._set_card(self.c2, t2, now)
        self._clear_target()

    def update_fired(self, now):
        """起播后检测 mpv 播完，自动复位（停最后一帧、推进到下一项）。"""
        if self.role in ("video", "both") and not self.cfg.dry_run and self.player is not None:
            try:
                if self.player.reached_eof():
                    self._auto_reset()
            except Exception:
                pass

    def _auto_reset(self):
        self._reset_to_idle(advance=True)

    def _auto_reset_fl(self):
        """FL 端起播后自动复位，省掉按 Esc。"""
        if self.state == "fired":
            self._reset_to_idle(advance=True)

    def _reset_to_idle(self, advance):
        self.has_fired = False
        if advance and self.playlist:
            if self.playlist_index < len(self.playlist) - 1:
                self.playlist_index += 1
            else:
                self.playlist_index = len(self.playlist)
        self.state = "idle"
        self.cd.config(text="00:00.0", fg=DIM)
        if self._playlist_done():
            self.set_status("已播完", DIM)
            self.set_msg("按 Esc 退出程序", MUTED)
        elif self.playlist and self._current_item():
            self.set_status("待就位", DIM)
            self.set_msg("第 %d/%d 项已就绪 · Enter 启动"
                         % (self.playlist_index + 1, len(self.playlist)), MUTED)
        else:
            self.set_status("待就位", DIM)
            self.set_msg("Enter 就位 · Esc 取消", MUTED)
        self.refresh_buttons()
        self.update_idle(time.time())
        self._update_playlist_display()
        self._sync_hint()

    # ---------------------------------------------------------------- 操作

    def arm(self):
        if self.state == "fired" and self.role in ("video", "both"):
            # 视频其实已经播完、自动复位还差一拍：先复位，让这次 Enter 有效
            try:
                finished = self.player is None or self.player.reached_eof()
            except Exception:
                finished = True
            if finished:
                self._reset_to_idle(advance=True)
        if self.state != "idle":
            return
        if self._playlist_done():
            return                      # 全部播完：Enter 不再重复触发
        item = self._current_item() if self.role in ("video", "both") else ""
        if self.role in ("video", "both"):
            if item:
                if not self.cfg.dry_run and not self.load_video(item):
                    return
                self._update_playlist_display()
            elif not self.video_ready:
                self.set_msg("还没有可用的视频，先在“播放列表…”里添加", AMBER)
                return
            elif self.player is not None:
                try:
                    self.player.reset()      # 回到 0 帧并暂停，下一次才能从头播
                except Exception:
                    pass
        now = time.time()
        # 直接取会被使用的时点（太近的格子自动跳过，不再显示"太近"提示）
        t1 = sp.next_slot(now, self.seconds, GUI_LEAD)
        t2 = sp.next_slot(t1, self.seconds, 0.001)
        target = t1

        self.next1, self.next2 = t1, t2
        self.target = target
        self.eff = (target + self.cfg.offset_ms / 1000.0
                    + self._clock_correction())
        self.focused = False

        self._set_card(self.c1, t1, now, highlight=True)
        self._set_card(self.c2, t2, now)
        self._mark_target(self.c1)

        self.set_msg("等待 %s · 按 Esc 取消"
                     % time.strftime("%H:%M:%S", time.localtime(target)), ACCENT)

        if self.role in ("fl", "both") and self.midi is None:
            self.fl_hwnd = sp.find_window(self.cfg.fl_window)
            if not self.fl_hwnd:
                self.set_msg("找不到 FL 窗口，起播时可能发不出按键", AMBER)

        self.state = "armed"
        self.set_status("已就位", ACCENT)
        self.refresh_buttons()
        self.cd.config(fg=TEXT)
        if (self.role in ("video", "both") and not self.compact
                and not self._separate_screen()):
            try:
                self.root.iconify()      # 单屏：主界面让位给画面，倒计时改在右下角
            except Exception:
                pass
        self._sync_hint()

    def update_armed(self, now):
        remain = self.eff - now
        if remain <= 0.05:
            while time.time() < self.eff:
                pass
            self.fire()
            return True

        if remain <= 0.35 and not self.focused:
            self.focused = True
            if self.role in ("fl", "both") and self.midi is None and self.fl_hwnd:
                sp.force_foreground(self.fl_hwnd)

        if remain <= 3.0 and self.state == "armed":
            self.state = "prepare"
            self.set_status("准备", AMBER)
            self.cd.config(fg=AMBER)
            self.set_msg("即将起播，Esc 即将失效", AMBER)

        self.cd.config(text=fmt_cd(remain))
        if self.overlay_visible:
            self.ov_cd.config(text=fmt_cd(remain),
                              fg=(AMBER if self.state == "prepare" else ACCENT))
        # 就位后目标已锁定，不再倒计"作废"；第二张卡仍显示可用倒计时
        self.c1["sub"].config(text="已锁定", fg=DIM)
        self.c2["sub"].config(
            text="%.1f 秒内可用" % max(0.0, self.next2 - GUI_LEAD - now), fg=DIM)
        return False

    def fire(self):
        t0 = time.time()
        self.has_fired = True
        late = t0 - self.eff
        if late > 0.5:
            self.state = "error"
            self.set_status("错过", RED)
            self.cd.config(text="00:00.0", fg=RED)
            self.set_msg("晚了 %.2f 秒，已放弃本次触发" % late, RED)
            self.hide_overlay()
            self._hide_idle_hint()
            self.bring_front()
            self.refresh_buttons()
            return

        ok = True
        if not self.cfg.dry_run:
            if self.role in ("fl", "both"):
                ok = sp.fl_fire(self.cfg, self.midi, self.fl_hwnd) and ok
            if self.player is not None:
                self.player.fire()
        dt = (time.time() - t0) * 1000.0

        if ok:
            self.state = "fired"
            self.set_status("已起播", GREEN)
            self.cd.config(text="00:00.0", fg=GREEN)
            self.set_msg("起播 %s · 调用 %.0f ms · 偏差 %+.0f ms · Esc 复位"
                         % (sp.fmt(t0), dt, (t0 - self.eff) * 1000.0), GREEN)
        else:
            self.state = "error"
            self.set_status("起播失败", RED)
            self.cd.config(text="00:00.0", fg=RED)
            self.set_msg("触发没有完成，看窗口提示", RED)
        # 视频已经开始，浮窗/右下角提示都收起；失败则把主界面恢复出来看错误
        self.hide_overlay()
        self._hide_idle_hint()
        if not ok:
            self.bring_front()
        self.refresh_buttons()
        if ok and self.role in ("video", "both"):
            self.root.after(600, self._minimize_after_start)
        if ok and self.role == "fl":
            self.root.after(int(FL_RESET_DELAY * 1000), self._auto_reset_fl)

    def _minimize_after_start(self):
        if self.state != "fired":
            return
        if self.compact or self._separate_screen():
            return                       # 双屏/精简：主界面保持原样
        try:
            self.root.iconify()
            boot_note("minimized after start")
        except Exception:
            pass

    def cancel(self):
        self.hide_overlay()
        self._hide_idle_hint()
        if not self.compact:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            except Exception:
                pass
        if self.state in ("armed", "prepare"):
            self.has_fired = False
            self.state = "idle"
            self.set_status("已取消", DIM)
            self.cd.config(text="00:00.0", fg=DIM)
            self.set_msg("已取消 · Enter 重新就位", MUTED)
            self.refresh_buttons()
            self.update_idle(time.time())
            self._update_playlist_display()
            self._sync_hint()
        elif self.state in ("fired", "error"):
            self._reset_to_idle(advance=True)

    # ---------------------------------------------------------------- 结束播放

    def _find_mpv_window(self):
        """找 mpv 的顶层窗口（它的窗口类名就是 mpv）。"""
        if sys.platform != "win32":
            return None
        try:
            u = ctypes.windll.user32
            u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p,
                                        ctypes.c_int]
            found = []

            @sp.WNDENUMPROC
            def cb(hwnd, _lparam):
                try:
                    buf = ctypes.create_unicode_buffer(64)
                    u.GetClassNameW(hwnd, buf, 64)
                    if buf.value.lower() == "mpv" and u.IsWindowVisible(hwnd):
                        found.append(hwnd)
                        return False
                except Exception:
                    pass
                return True

            u.EnumWindows(cb, 0)
            return found[0] if found else None
        except Exception:
            return None

    def _fade_out_mpv(self, steps=10, delay=0.03):
        """尽量让 mpv 窗口淡出；硬件全屏下可能无效，失败就忽略。"""
        if sys.platform != "win32":
            return
        hwnd = self._find_mpv_window()
        if not hwnd:
            return
        try:
            u = ctypes.windll.user32
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                         ctypes.c_long]
            u.SetLayeredWindowAttributes.argtypes = [wintypes.HWND,
                                                     ctypes.c_uint,
                                                     ctypes.c_ubyte,
                                                     ctypes.c_uint]
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            LWA_ALPHA = 0x2
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
            for i in range(steps):
                alpha = int(255 * (1.0 - (i + 1) / float(steps)))
                u.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)
                self.root.update_idletasks()
                time.sleep(delay)
        except Exception:
            pass

    def end_playback(self):
        """鼠标专用：二次确认后关闭 mpv 并退出程序。"""
        try:
            ok = self._dialog(messagebox.askyesno, "结束播放",
                              "确定关闭视频并退出程序？", parent=self.root)
        except Exception:
            ok = False
        if not ok:
            return
        self._closing = True
        try:
            self._fade_out_mpv()          # 先淡出，失败也不影响后面
        except Exception:
            pass
        try:
            if self.player is not None:
                self.player.stop()
        except Exception:
            pass
        try:
            sp.close_existing_mpv()       # 连 mpv.exe 一起清掉
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _handle_esc(self):
        """Esc：全部播完后直接退出程序；其它情况按原逻辑取消/复位。"""
        self._arm_pending = False
        if self.state == "idle" and self._playlist_done():
            self._on_close()
        else:
            self.cancel()


def main():
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    enable_dpi()
    topmost = "--no-topmost" not in sys.argv
    compact = "--compact" in sys.argv
    argv = [a for a in sys.argv[1:]
            if a not in ("--no-topmost", "--compact")]
    cfg = sp.parse_args(argv)
    if "--screen" not in argv:
        saved = read_last_screen()
        if saved is not None and 0 <= saved < monitor_count():
            cfg.screen = saved
        elif monitor_count() == 1:
            cfg.screen = 0
    if "--grid" not in argv:
        saved_grid = read_last_grid()
        if saved_grid:
            cfg.grid = ",".join(str(s) for s in saved_grid)
    if "--offset-ms" not in argv:
        saved_off = read_last_offset()
        if saved_off is not None:
            cfg.offset_ms = saved_off
    if "--stretch" not in argv:
        cfg.stretch = read_last_stretch()
    boot_note("args " + " ".join(argv) + ("  [compact]" if compact else ""))
    if not single_instance(cfg.role):
        boot_note("already running, exit")
        return 0

    if cfg.file and not os.path.isfile(cfg.file):
        print("视频文件不存在：%s%s" % (cfg.file, sp.suggest_file(cfg.file)))
        return 2

    root = tk.Tk()
    boot_note("tk ready")
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    w, h = int(800 * scale), int(560 * scale)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
    root.minsize(int(720 * scale), int(520 * scale))
    if compact:
        root.withdraw()

    app = App(root, cfg, topmost, compact=compact)
    root.after(50, app.start)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
