"""SFTP 与端口转发的真实链路验证。

覆盖：
- SFTP：上传（含中文）→ 列目录 → 下载比对 → 重命名 → 删除
- 本地转发（-L）：本机端口 → 远端 127.0.0.1:22，应拿到 SSH banner
- 动态转发（-D）：本机 SOCKS5 → 远端 127.0.0.1:22，应拿到 SSH banner
- 远端转发（-R）：服务器监听端口 → 本机回环 echo 服务

运行::

    .venv\\Scripts\\python.exe tests\\sftp_forward_smoke.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QCoreApplication

from sshmanager.credentials import CredentialStore
from sshmanager.forwarding import KIND_DYNAMIC, KIND_LOCAL, KIND_REMOTE, ForwardManager, ForwardRule
from sshmanager.hosts import Host
from sshmanager.sftpcore import SftpClient
from sshmanager.sshclient import SSHClient

MARKER = "PYSSH_SFTP_OK"
PAYLOAD = "hello from PySSH Manager · 中文内容 · " + MARKER + "\n"

LOCAL_ECHO_PORT = 18991
REMOTE_LISTEN_PORT = 19022


def start_echo_server(port: int) -> threading.Event:
    """本机回环上的小服务：收到数据后回 ``ECHO:<原样>``。"""

    ready = threading.Event()

    def handle(connection: socket.socket) -> None:
        try:
            connection.settimeout(10)
            data = connection.recv(1024)

            if data:
                connection.sendall(b"ECHO:" + data)
        except OSError:
            pass
        finally:
            connection.close()

    def serve() -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(4)
        server.settimeout(40)
        ready.set()

        try:
            while True:
                connection, _ = server.accept()
                threading.Thread(target=handle, args=(connection,), daemon=True).start()
        except OSError:
            pass
        finally:
            server.close()

    threading.Thread(target=serve, daemon=True).start()

    return ready


def socks5_connect(proxy_port: int, host: str, port: int, timeout: float = 8.0):
    """走 SOCKS5 代理建立连接（无认证 CONNECT）。"""

    try:
        proxy = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    except OSError:
        return None

    try:
        proxy.settimeout(timeout)
        proxy.sendall(b"\x05\x01\x00")

        if proxy.recv(2) != b"\x05\x00":
            proxy.close()
            return None

        address = host.encode("utf-8")
        proxy.sendall(b"\x05\x01\x00\x03" + bytes([len(address)]) + address + struct.pack("!H", port))
        header = proxy.recv(4)

        if len(header) < 4 or header[1] != 0x00:
            proxy.close()
            return None

        if header[3] == 0x01:
            proxy.recv(4)
        elif header[3] == 0x03:
            proxy.recv(proxy.recv(1)[0])
        elif header[3] == 0x04:
            proxy.recv(16)

        proxy.recv(2)

        return proxy
    except OSError:
        proxy.close()
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--local-port", type=int, default=19081)
    parser.add_argument("--socks-port", type=int, default=19080)
    parser.add_argument("--sftp-only", action="store_true", help="只跑 SFTP 部分")
    args = parser.parse_args()

    QCoreApplication(sys.argv[:1])

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

    client.connect(
        hostname=args.host,
        port=args.port,
        username=args.username,
        password=password,
        verify_host_key=True,
    )
    print("1. SSH 连接成功", flush=True)

    # ==================================================================
    # SFTP
    # ==================================================================
    sftp = SftpClient(client)
    sftp.open()
    home = sftp.home()
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

        uploaded, _resumed = sftp.upload(source, remote_file)
        results["upload"] = uploaded == source.stat().st_size
        print(f"2. 上传 {uploaded} 字节到 {remote_file}", flush=True)

        entries = {entry.name: entry for entry in sftp.listdir(remote_dir)}
        results["list"] = "hello.txt" in entries
        results["list_size"] = entries.get("hello.txt") is not None and entries["hello.txt"].size == uploaded
        print(f"3. 远端列目录：{[(e.name, e.size_text) for e in entries.values()]}", flush=True)

        downloaded = workspace_path / "downloaded.txt"
        sftp.download(remote_file, downloaded)
        content = downloaded.read_text(encoding="utf-8")
        results["download_content"] = content == PAYLOAD and MARKER in content
        print(f"4. 下载比对：{'一致' if content == PAYLOAD else '不一致'}", flush=True)

        sftp.rename(remote_file, renamed_file)
        names = {entry.name for entry in sftp.listdir(remote_dir)}
        results["rename"] = "renamed.txt" in names and "hello.txt" not in names
        print(f"5. 重命名后：{sorted(names)}", flush=True)

        sftp.remove(renamed_file)
        results["remove_file"] = all(entry.name != "renamed.txt" for entry in sftp.listdir(remote_dir))

        sftp.remove(remote_dir, is_dir=True)
        results["remove_dir"] = remote_dir.rsplit("/", 1)[-1] not in {
            entry.name for entry in sftp.listdir(home)
        }
        print("6. 清理完成（文件 + 目录）", flush=True)

    sftp.close()

    # ==================================================================
    # 端口转发
    # ==================================================================
    if not args.sftp_only:
        manager = ForwardManager(client)

        # --- 本地转发 (-L) ---
        local_rule = ForwardRule(
            kind=KIND_LOCAL,
            bind="127.0.0.1",
            local_port=args.local_port,
            remote_host="127.0.0.1",
            remote_port=22,
        )
        local_started = manager.start(0, local_rule)
        banner = b""

        if local_started:
            time.sleep(0.4)

            try:
                with socket.create_connection(("127.0.0.1", args.local_port), timeout=6) as connection:
                    connection.settimeout(8)
                    banner = connection.recv(128)
            except OSError as error:
                notes.append(f"本地转发连接失败：{error}")

        results["local_forward"] = local_started and banner.startswith(b"SSH-2.0")
        print(f"7. 本地转发 -L：{banner[:30]!r}", flush=True)

        # --- 动态转发 (-D, SOCKS5) ---
        socks_started = manager.start(
            1,
            ForwardRule(kind=KIND_DYNAMIC, bind="127.0.0.1", local_port=args.socks_port),
        )
        socks_banner = b""

        if socks_started:
            time.sleep(0.4)
            proxy = socks5_connect(args.socks_port, "127.0.0.1", 22)

            if proxy is None:
                notes.append("SOCKS5 握手失败")
            else:
                try:
                    proxy.settimeout(8)
                    socks_banner = proxy.recv(128)
                except OSError as error:
                    notes.append(f"SOCKS5 读取失败：{error}")
                finally:
                    proxy.close()

        results["dynamic_socks5"] = socks_started and socks_banner.startswith(b"SSH-2.0")
        print(f"8. 动态转发 -D（SOCKS5）：{socks_banner[:30]!r}", flush=True)

        # --- 远端转发 (-R) ---
        echo_ready = start_echo_server(LOCAL_ECHO_PORT)

        if not echo_ready.wait(3):
            notes.append("本机 echo 服务未就绪")

        remote_started = manager.start(
            2,
            ForwardRule(
                kind=KIND_REMOTE,
                bind="127.0.0.1",
                remote_port=REMOTE_LISTEN_PORT,
                remote_host="127.0.0.1",
                local_port=LOCAL_ECHO_PORT,
            ),
        )
        time.sleep(0.8)

        remote_reply = b""

        if remote_started:
            probe = (
                "P=$(command -v python3 || command -v python); "
                "\"$P\" -c \"import socket,sys;p=%d;s=socket.create_connection(('127.0.0.1',p),5);"
                "s.sendall(b'PING');s.shutdown(socket.SHUT_WR);"
                "sys.stdout.write(s.recv(40).decode('utf-8','replace'));sys.stdout.flush()\""
            ) % REMOTE_LISTEN_PORT

            stdin, stdout, stderr = client.exec_command(probe, timeout=15)
            remote_reply = stdout.read().strip()
            error_text = stderr.read().decode("utf-8", errors="replace").strip()

            if error_text:
                notes.append(f"远端探针 stderr：{error_text[:120]}")

        results["remote_forward"] = remote_started and b"ECHO:PING" in remote_reply
        print(f"9. 远端转发 -R：服务器回包 {remote_reply[:40]!r}", flush=True)

        manager.stop_all()
        results["all_stopped"] = manager.active_count() == 0

    client.close()

    # ------------------------------------------------------------------

    print("--- 判定 ---", flush=True)

    for name, value in results.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    for note in notes:
        print("note:", note, flush=True)

    ok = all(results.values())
    print("SFTP_FORWARD_SMOKE_OK" if ok else "SFTP_FORWARD_SMOKE_FAILED", flush=True)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
