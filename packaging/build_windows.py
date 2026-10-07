"""一键打包 Windows 程序（PyInstaller）。

会依次做：

1. 生成图标（``packaging/icon.ico``，没有才生成）
2. 打包**目录版** → ``dist/PySSH-Manager/PySSH-Manager.exe``（推荐分发，启动快）
3. 可选打包**单文件版** → ``dist/PySSH-Manager.exe``
4. 用打好的 exe 跑一次**自检**（GUI / ConPTY 本地终端 / SSH / SFTP），结果写进
   ``dist/selftest-*.json``，失败会以非零码退出
5. 可选打 zip，便于直接上传 Release

用法::

    .venv\\Scripts\\python.exe packaging\\build_windows.py                 # 目录版 + 自检
    .venv\\Scripts\\python.exe packaging\\build_windows.py --onefile       # 额外打单文件版
    .venv\\Scripts\\python.exe packaging\\build_windows.py --onefile --zip # 再打 zip
    .venv\\Scripts\\python.exe packaging\\build_windows.py --host 1.2.3.4 --username root --password pw

只打包不验证可加 ``--no-selftest``。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "PySSH-Manager.spec"
DIST = ROOT / "dist"

# 控制台可能是 cp1252/GBK，打中文/✔ 会抛 UnicodeEncodeError（CI 上踩过）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def read_version() -> str:
    import re

    text = (ROOT / "sshmanager" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)

    return match.group(1) if match else "0.0.0"


def run(command: list[str], **kwargs) -> int:
    print("»", " ".join(str(part) for part in command), flush=True)

    return subprocess.call(command, **kwargs)


def build(spec: Path, workpath: str, onefile: bool) -> int:
    env = dict(os.environ)
    env["PYSSH_ONEFILE"] = "1" if onefile else "0"

    return run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST),
            "--workpath",
            str(ROOT / workpath),
            str(spec),
        ],
        env=env,
    )


def selftest(exe: Path, host: str, port: int, username: str, password: str) -> bool:
    report = DIST / f"selftest-{exe.parent.name if exe.parent != DIST else 'onefile'}.json"

    if report.exists():
        report.unlink()

    args = [str(exe), "--selftest", str(report)]

    if host:
        args += ["--host", host, "--port", str(port), "--username", username, "--password", password]
    else:
        args += ["--no-autoconnect"]

    print("» 自检：", " ".join(args), flush=True)

    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")  # 不弹窗

    code = subprocess.call(args, env=env)
    print(f"  自检退出码 {code}，报告：{report}", flush=True)

    return code == 0


def make_zip(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(source.parent))
        else:
            archive.write(source, source.name)

    print(f"» 已生成 {target}（{target.stat().st_size / 1024 / 1024:.1f} MB）", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 PySSH Manager 为 Windows 程序")
    parser.add_argument("--onefile", action="store_true", help="额外打一个单文件 exe")
    parser.add_argument("--zip", action="store_true", help="打包完成后生成 zip")
    parser.add_argument("--no-selftest", action="store_true", help="跳过打包后的自检")
    parser.add_argument("--host", default="", help="自检时连接的主机（不给就只做离线检查）")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("✖ 只能在 Windows 上打包 Windows 程序", file=sys.stderr)
        return 2

    icon = ROOT / "packaging" / "icon.ico"

    if not icon.exists():
        run([sys.executable, str(ROOT / "packaging" / "make_icon.py")])

    version = read_version()
    ok = True

    # 1) 目录版
    if build(SPEC, "build/pyinstaller", onefile=False) != 0:
        return 1

    onedir_exe = DIST / "PySSH-Manager" / "PySSH-Manager.exe"
    print(f"✔ 目录版：{onedir_exe}", flush=True)

    if not args.no_selftest:
        ok = selftest(onedir_exe, args.host, args.port, args.username, args.password) and ok

    if args.zip:
        make_zip(onedir_exe.parent, DIST / f"PySSH-Manager-{version}-windows-x64.zip")

    # 2) 单文件版
    if args.onefile:
        if build(SPEC, "build/pyinstaller-onefile", onefile=True) != 0:
            return 1

        onefile_exe = DIST / "PySSH-Manager.exe"
        print(f"✔ 单文件版：{onefile_exe}（{onefile_exe.stat().st_size / 1024 / 1024:.1f} MB）", flush=True)

        if not args.no_selftest:
            ok = selftest(onefile_exe, args.host, args.port, args.username, args.password) and ok

        if args.zip:
            make_zip(onefile_exe, DIST / f"PySSH-Manager-{version}-windows-x64-portable.zip")

    print("✔ 打包完成" if ok else "✖ 打包完成，但自检未通过", flush=True)

    # 顺手清掉 PyInstaller 的中间产物
    for cache in ROOT.glob("build/pyinstaller*"):
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
