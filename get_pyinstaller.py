# -*- coding: utf-8 -*-
"""绕过 pip 的安装步骤：从 PyPI 直接下载 wheel 并解压到 _vendor。

（这台机器上 pip 的解包/写 .metadata 会 PermissionError，手动写同一个文件却正常。）
"""
import json
import os
import sys
import urllib.request
import zipfile

PKGS = [
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "pefile",
    "pywin32-ctypes",
    "packaging",
    "setuptools",
]

VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vendor")
DL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dl")


def score(name):
    if not name.endswith(".whl"):
        return -1
    s = 0
    if "win_amd64" in name:
        s += 4
    elif "any" in name:
        s += 3
    else:
        return -1
    if "cp313" in name:
        s += 8
    elif "abi3" in name:
        s += 5
    elif "cp3" in name:
        s += 2
    if "py3-none-any" in name:
        s += 2
    if "musllinux" in name or "manylinux" in name or "macosx" in name:
        return -1
    return s


def fetch(pkg):
    url = "https://pypi.org/pypi/%s/json" % pkg
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    files = data["releases"][data["info"]["version"]]
    best = max(files, key=lambda f: score(f["filename"]))
    if score(best["filename"]) <= 0:
        raise SystemExit("没有可用的 wheel: %s" % pkg)
    dst = os.path.join(DL, best["filename"])
    if not os.path.isfile(dst):
        urllib.request.urlretrieve(best["url"], dst)
    return dst


def main():
    os.makedirs(VENDOR, exist_ok=True)
    os.makedirs(DL, exist_ok=True)
    for pkg in PKGS:
        whl = fetch(pkg)
        with zipfile.ZipFile(whl) as z:
            z.extractall(VENDOR)
        print("ok  %-28s %s" % (pkg, os.path.basename(whl)))
    print("vendor:", VENDOR)


if __name__ == "__main__":
    sys.exit(main())
