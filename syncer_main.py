# -*- coding: utf-8 -*-
"""打包版入口：一个 exe，按参数启动不同角色。

  Syncer.exe --role fl                 放节拍器/工程的那台
  Syncer.exe --role video              放视频的那台
  Syncer.exe --role video --compact    精简模式（只有右下角提示）
  Syncer.exe --calibrate               对时工具

不带参数直接双击时，弹一个小窗口让你选。
"""

import os
import sys
import time
import traceback

BASE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)
sys.path.insert(0, BASE)

UI_FONT = ("Microsoft YaHei UI", "微软雅黑", "Segoe UI")


def note(msg):
    try:
        with open(os.path.join(BASE, "boot.log"), "a", encoding="utf-8") as f:
            f.write("%s  exe | %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def show_error():
    try:
        with open(os.path.join(BASE, "error.log"), "a", encoding="utf-8") as f:
            f.write("\n%s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                    traceback.format_exc()))
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Syncer 启动失败", traceback.format_exc())
        root.destroy()
    except Exception:
        pass


def choose_mode():
    """不带参数时的小选择窗口，返回要追加的参数列表。"""
    import tkinter as tk

    picked = []

    def pick(*args):
        picked.extend(args)
        try:
            root.destroy()
        except Exception:
            pass

    root = tk.Tk()
    root.title("Syncer")
    root.configure(bg="#0E1116")
    root.attributes("-topmost", True)
    tk.Label(root, text="选择要打开的程序", bg="#0E1116", fg="#E9EEF5",
             font=(UI_FONT[0], 12, "bold")).pack(padx=28, pady=(20, 12))
    for label, args in (
            ("FL 端（放节拍器 / 工程）", ("--role", "fl")),
            ("视频端（完整界面）", ("--role", "video")),
            ("视频端-精简（只有右下角提示）", ("--role", "video", "--compact")),
            ("对时工具", ("--calibrate",))):
        tk.Button(root, text=label, command=lambda a=args: pick(*a),
                  font=(UI_FONT[0], 10), bg="#161B22", fg="#E9EEF5",
                  activebackground="#202833", activeforeground="#E9EEF5",
                  relief="flat", bd=0, highlightthickness=1,
                  highlightbackground="#2A313C",
                  padx=18, pady=10, cursor="hand2").pack(fill="x", padx=24,
                                                         pady=4)
    tk.Button(root, text="取消", command=root.destroy,
              font=(UI_FONT[0], 9), bg="#0E1116", fg="#8B95A5",
              activebackground="#0E1116", activeforeground="#8B95A5",
              relief="flat", bd=0, highlightthickness=0).pack(pady=(6, 14))
    root.mainloop()
    return list(picked)


def main():
    args = sys.argv[1:]
    if not args:
        args = choose_mode()
        if not args:
            return 0
        sys.argv = [sys.argv[0]] + args

    if any(a in ("--calibrate", "--calib") for a in args):
        sys.argv = [sys.argv[0]] + [a for a in args
                                    if a not in ("--calibrate", "--calib")]
        import calibrate
        return calibrate.main()

    import syncplay_gui
    return syncplay_gui.main()


note("start")
try:
    main()
    note("exit")
except SystemExit:
    raise
except Exception:
    note("crash")
    show_error()
