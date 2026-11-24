# 打包成免安装 exe（含便携 mpv）
#
# 用法（在项目目录里）：
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#
# 产物：
#   dist\VJSyncer\         解压即用的程序目录（exe + 自带 mpv + 4 个快捷方式）
#   VJSyncer_vX.Y.Z_win64.zip 可以直接发给别人
#
# 首次运行会自动下载 PyInstaller 到 _vendor（不污染系统 Python）。

param(
    [string]$MpvDir = "C:\Program Files\MPV Player"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$version = (Select-String -Path syncplay_gui.py -Pattern 'VERSION = "(v[\d.]+)"').Matches[0].Groups[1].Value
"打包版本: $version"

# 1) PyInstaller
$env:PYTHONPATH = Join-Path $root "_vendor"
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root "_pyi"
New-Item -ItemType Directory -Force $env:PYINSTALLER_CONFIG_DIR | Out-Null
if (-not (Test-Path (Join-Path $root "_vendor\PyInstaller"))) {
    "下载 PyInstaller ..."
    python (Join-Path $root "get_pyinstaller.py")
    if ($LASTEXITCODE -ne 0) { throw "下载 PyInstaller 失败" }
}

# 2) 打包
$manifest = Join-Path $root "app.manifest"
python -m PyInstaller --noconfirm --clean --windowed --onedir --name VJSyncer --manifest "$manifest" `
    --distpath dist --workpath build --specpath build `
    --hidden-import calibrate --hidden-import syncplay_gui --hidden-import syncplay `
    syncer_main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$out = Join-Path $root "dist\VJSyncer"
if (-not (Test-Path (Join-Path $out "VJSyncer.exe"))) { throw "没找到 VJSyncer.exe" }

# 3) 自带 mpv
$mpvOut = Join-Path $out "mpv"
New-Item -ItemType Directory -Force $mpvOut | Out-Null
foreach ($f in @("mpv.exe", "mpv.com", "d3dcompiler_43.dll")) {
    $src = Join-Path $MpvDir $f
    if (Test-Path $src) { Copy-Item $src $mpvOut -Force }
}
if (-not (Test-Path (Join-Path $mpvOut "mpv.exe"))) {
    throw "没找到 mpv.exe，请检查 -MpvDir 参数（当前: $MpvDir）"
}
$mpvVer = (Get-Item (Join-Path $mpvOut "mpv.exe")).VersionInfo.FileVersion
@"
mpv —— 随包附带（程序会自动优先使用这个版本）

版本：$mpvVer
许可证：GPLv2+ / LGPLv2.1+（部分组件为 GPL）
主页：https://mpv.io
源码：https://github.com/mpv-player/mpv
许可全文：https://github.com/mpv-player/mpv/blob/master/Copyright
"@ | Set-Content (Join-Path $mpvOut "许可证说明.txt") -Encoding UTF8

# 4) 快捷方式
$ws = New-Object -ComObject WScript.Shell
function New-Lnk([string]$name, [string]$argline) {
    $lnk = $ws.CreateShortcut((Join-Path $out "$name.lnk"))
    $lnk.TargetPath = Join-Path $out "VJSyncer.exe"
    $lnk.Arguments = $argline
    $lnk.WorkingDirectory = $out
    $lnk.IconLocation = (Join-Path $out "VJSyncer.exe") + ",0"
    $lnk.Save()
}
New-Lnk "FL端"        "--role fl"
New-Lnk "视频端"      "--role video"
New-Lnk "视频端-精简" "--role video --compact"
New-Lnk "对时"        "--calibrate"

# 5) 压缩（杀软可能正在扫描刚复制进来的 mpv.exe，失败就等几秒重试）
$zip = Join-Path $root ("VJSyncer_{0}_win64.zip" -f $version)
for ($i = 1; $i -le 4; $i++) {
    try {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path $out -DestinationPath $zip -CompressionLevel Optimal
        break
    } catch {
        if ($i -eq 4) { throw }
        "压缩被占用，$($i) 次重试……"
        Start-Sleep -Seconds 3
    }
}
"完成: $zip  ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"

# 6) 清理：只保留这次的 zip，删掉构建中间目录
$keep = Split-Path $zip -Leaf
Get-ChildItem $root -Filter "*_win64.zip" | Where-Object { $_.Name -ne $keep } |
    ForEach-Object { "删除旧包: $($_.Name)"; Remove-Item $_.FullName -Force }
Remove-Item (Join-Path $root "build") -Recurse -Force -ErrorAction SilentlyContinue
