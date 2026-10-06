"""SFTP 与端口转发的真实链路验证。

覆盖：连接 → 上传（含中文内容）→ 远端列目录 → 下载并比对 → 重命名 → 定向下载 → 删除，
以及本地端口转发：本机端口 → 经 SSH → 远端 127.0.0.1:80，并校验回传内容。

运行::

    .venv\\Scripts\\python.exe tests\\sftp_forward_smoke.py --host 192.168.206.130 --password 123456
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication, QTimer

from sshmanager.credentials import CredentialStore
from sshmanager.forwarding import ForwardManager, ForwardRule
from sshmanager.hosts import Host
from sshmanager.sftpcore import SftpClient
from sshmanager.sshclient import SSHClient

MARKER = "PYSSH_SFTP_OK"
PAYLOAD = "hello from PySSH Manager · 中文内容 · " + MARKER + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.206.130")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--remote-port", type=int, default=22, help="远端目标端口（默认 22，sshd 必然在监听）")
    args = parser.parse_args()

    app = QCoreApplication(sys.argv[:1])  # ForwardManager 需要事件循环环境

    password = args.password

    if not password:
        store = CredentialStore()

        for key in list(store._cache):  # noqa: SLF001 - 冒烟脚本
            password = store.get(key) or ""
            break

    if not password:
        print("NO_CREDENTIAL: 请加 --password")
        return 2

    host = Host(label="冒烟", hostname=args.host, port=args.port, username=args.username)
    client = SSHClient(host)

    results: dict[str, bool] = {}
    notes: list[str] = []

    try:
        client.connect(
            hostname=args.host,
            port=args.port,
            username=args.username,
            password=password,
            verify_host_key=True,
        )
        print("1. SSH 连接成功", flush=True)

        sftp = SftpClient(client)
        sftp.open()
        home = sftp.home()
        print(f"2. SFTP 通道就绪，家目录 {home}", flush=True)

        remote_dir = f"{home}/pyssh-sftp-test"
        remote_file = f"{remote_dir}/hello.txt"
        renamed_file = f"{remote_dir}/renamed.txt"

        try:
            sftp.remove(remote_dir, is_dir=True)
        except Exception:
            pass

        sftp.mkdir(remote_dir)
        results["mkdir"] = True

        with tempfile.TemporaryDirectory(prefix="pyssh-sftp-") as workspace:
            workspace_path = Path(workspace)
            source = workspace_path / "hello.txt"
            source.write_text(PAYLOAD, encoding="utf-8")

            uploaded = sftp.upload(source, remote_file)
            print(f"3. 上传 {uploaded} 字节", flush=True)
            results["upload"] = uploaded == source.stat().st_size

            entries = {entry.name: entry for entry in sftp.listdir(remote_dir)}
            results["list"] = "hello.txt" in entries
            results["list_size"] = entries.get("hello.txt") is not None and entries["hello.txt"].size == uploaded
            print(f"4. 远端列目录：{[(e.name, e.size_text) for e in entries.values()]}", flush=True)

            downloaded = workspace_path / "downloaded.txt"
            sftp.download(remote_file, downloaded)
            content = downloaded.read_text(encoding="utf-8")
            results["download_content"] = content == PAYLOAD and MARKER in content
            print(f"5. 下载并比对内容：{'一致' if content == PAYLOAD else '不一致'}", flush=True)

            sftp.rename(remote_file, renamed_file)
            names = {entry.name for entry in sftp.listdir(remote_dir)}
            results["rename"] = "renamed.txt" in names and "hello.txt" not in names
            print(f"6. 重命名后目录：{sorted(names)}", flush=True)

            sftp.remove(renamed_file)
            results["remove_file"] = all(entry.name != "renamed.txt" for entry in sftp.listdir(remote_dir))

            sftp.remove(remote_dir, is_dir=True)
            results["remove_dir"] = remote_dir.rsplit("/", 1)[-1] not in {
                entry.name for entry in sftp.listdir(home)
            }
            print("7. 清理完成（文件 + 目录）", flush=True)
    except Exception as error:  # noqa: BLE001
        notes.append(f"SFTP 阶段失败：{error}")
        print(f"SFTP_FAILED: {error}", flush=True)
    finally:
        sftp.close()

    # ------------------------------------------------------------------
    # 端口转发：本机端口 → 经 SSH → 远端 127.0.0.1:22（sshd 一定在）
    # ------------------------------------------------------------------
    received: dict[str, bytes] = {}

    manager = ForwardManager(client)
    rule = ForwardRule(
        bind="127.0.0.1",
        local_port=args.local_port,
        remote_host="127.0.0.1",
        remote_port=args.remote_port,
    )

    started = manager.start(0, rule)
    print(f"8. 开启本地转发 {rule.summary} -> {started}", flush=True)

    banner = b""

    if started:
        time.sleep(0.4)

        try:
            with socket.create_connection(("127.0.0.1", args.local_port), timeout=6) as local:
                local.settimeout(8)
                banner = local.recv(256)
        except Exception as error:  # noqa: BLE001
            notes.append(f"转发连接失败：{error}")

    results["forward_started"] = started
    results["forward_banner"] = banner.startswith(b"SSH-2.0")
    print(f"9. 经转发收到的远端 banner：{banner[:40]!r}", flush=True)

    # 第二个连接，确认监听可以持续接受连接
    second = b""

    if started:
        try:
            with socket.create_connection(("127.0.0.1", args.local_port), timeout=6) as local:
                local.settimeout(8)
                second = local.recv(256)
        except Exception as error:  # noqa: BLE001
            notes.append(f"第二次转发连接失败：{error}")

    results["forward_reusable"] = second.startswith(b"SSH-2.0")
    print(f"10. 第二次连接 banner：{second[:40]!r}", flush=True)

    manager.stop_all()
    results["forward_stopped"] = manager.active_count() == 0
    client.close()

    # ------------------------------------------------------------------

    print("--- 判定 ---", flush=True)

    for name, value in results.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    for note in notes:
        print("note:", note, flush=True)

    ok = all(results.values()) and not notes
    print("SFTP_FORWARD_SMOKE_OK" if ok else "SFTP_FORWARD_SMOKE_FAILED", flush=True)

    QTimer.singleShot(0, app.quit)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
