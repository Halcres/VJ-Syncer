# -*- coding: utf-8 -*-
"""双击启动视频端（精简模式）：不显示主界面，只在右下角显示待播时间码。

和“视频端.pyw”共用全部参数与逻辑，只是多带一个 --compact。
"""

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
sys.argv = [sys.argv[0], "--role", "video", "--player", "mpv", "--compact"]


def note(msg):
    try:
        with open(os.path.join(HERE, "boot.log"), "a", encoding="utf-8") as f:
            f.write("%s  launcher %s | %s\n" % (time.strftime("%H:%M:%S"), sys.executable, msg))
    except OSError:
        pass


def show_error():
    try:
        with open(os.path.join(HERE, "error.log"), "a", encoding="utf-8") as f:
            f.write("\n%s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc()))
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


note("start")
try:
    import syncplay_gui
    note("imported")
    syncplay_gui.main()
    note("exit")
except Exception:
    note("crash")
    show_error()
