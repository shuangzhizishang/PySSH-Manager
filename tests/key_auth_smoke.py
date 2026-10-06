"""密钥认证真实链路验证：本地起一个 paramiko SSH 服务器，用私钥 + passphrase 连接。

只在本机回环端口上跑，不接触任何外部服务器。

运行::

    .venv\\Scripts\\python.exe tests\\key_auth_smoke.py
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paramiko
from paramiko.ssh_exception import SSHException

from sshmanager.hosts import Host
from sshmanager.sshclient import SSHClient

MARKER = "PYSSH_KEY_AUTH_OK"


class _Server(paramiko.ServerInterface):
    """只接受指定指纹的客户端公钥。"""

    def __init__(self, authorized_fingerprint: bytes) -> None:
        self.authorized_fingerprint = authorized_fingerprint
        self.shell_requested = threading.Event()

    def check_auth_publickey(self, username, key):  # noqa: N802
        if username == "keyuser" and key.get_fingerprint() == self.authorized_fingerprint:
            return paramiko.AUTH_SUCCESSFUL

        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):  # noqa: N802
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):  # noqa: N802
        return "publickey"

    def check_channel_request(self, kind, chanid):  # noqa: N802
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED

        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args, **kwargs):  # noqa: N802
        return True

    def check_channel_shell_request(self, channel):  # noqa: N802
        self.shell_requested.set()
        return True


def _serve_connection(client_socket, server_key, authorized_fingerprint: bytes, results: dict) -> None:
    transport = paramiko.Transport(client_socket)
    transport.add_server_key(server_key)
    server = _Server(authorized_fingerprint)

    try:
        transport.start_server(server=server)
    except SSHException as error:
        results["server_error"] = str(error)
        return

    channel = transport.accept(15)

    if channel is None:
        results["server_error"] = "客户端没有打开通道"
        transport.close()
        return

    server.shell_requested.wait(10)

    try:
        channel.sendall(f"\r\n{MARKER} key auth works\r\nkeyuser@localhost:~$ ".encode())
        time.sleep(1.5)
    except Exception:
        pass

    channel.close()
    transport.close()


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="pyssh-keyauth-"))
    results: dict = {}

    # paramiko 的 Ed25519Key 只能解析 OpenSSH 格式、不能直接生成，
    # 因此本地测试用 RSAKey.generate 造密钥。
    server_key = paramiko.RSAKey.generate(2048)
    client_key = paramiko.RSAKey.generate(2048)
    client_key_path = workspace / "id_rsa"
    client_key.write_private_key_file(str(client_key_path), password="passphrase-123")
    client_fingerprint = client_key.get_fingerprint()

    # 确认带 passphrase 的私钥能被重新加载（与 connect 的行为一致）
    reloaded = paramiko.RSAKey.from_private_key_file(str(client_key_path), password="passphrase-123")

    assert reloaded.get_fingerprint() == client_fingerprint

    # ------------------------------------------------------------------
    # 1) 正确的私钥 + passphrase 必须连上
    # ------------------------------------------------------------------
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    def accept_once() -> None:
        client_socket, _ = listener.accept()
        _serve_connection(client_socket, server_key, client_fingerprint, results)

    threading.Thread(target=accept_once, daemon=True).start()

    host = Host(label="密钥测试", hostname="127.0.0.1", port=port, username="keyuser", auth_type="key")
    client = SSHClient(host)
    banner = b""
    connected = False

    try:
        client.connect(
            hostname="127.0.0.1",
            port=port,
            username="keyuser",
            key_path=str(client_key_path),
            passphrase="passphrase-123",
            verify_host_key=False,
        )
        connected = True
        deadline = time.time() + 12

        while time.time() < deadline:
            chunk = client.recv()

            if chunk:
                banner += chunk

            if MARKER.encode() in banner:
                break
    except Exception as error:  # noqa: BLE001
        results["client_error"] = f"{type(error).__name__}: {error}"
    finally:
        client.close()
        listener.close()

    # ------------------------------------------------------------------
    # 2) 错误的 passphrase 必须失败
    # ------------------------------------------------------------------
    listener2 = socket.socket()
    listener2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener2.bind(("127.0.0.1", 0))
    listener2.listen(4)
    port2 = listener2.getsockname()[1]

    def accept_reject() -> None:
        client_socket, _ = listener2.accept()
        transport = paramiko.Transport(client_socket)
        transport.add_server_key(server_key)

        try:
            transport.start_server(server=_Server(client_fingerprint))
        except Exception:
            pass

        transport.close()

    threading.Thread(target=accept_reject, daemon=True).start()

    client2 = SSHClient(host)
    wrong_passphrase_failed = False

    try:
        client2.connect(
            hostname="127.0.0.1",
            port=port2,
            username="keyuser",
            key_path=str(client_key_path),
            passphrase="wrong-passphrase",
            verify_host_key=False,
            timeout=6,
            auth_timeout=6,
        )
    except Exception:
        wrong_passphrase_failed = True
    finally:
        client2.close()
        listener2.close()

    # ------------------------------------------------------------------

    text = banner.decode("utf-8", errors="replace")
    print("服务器端错误:", results.get("server_error"), flush=True)
    print("客户端错误:", results.get("client_error"), flush=True)
    print("收到内容:", repr(text[:80]), flush=True)

    checks = {
        "key_auth_connected": connected and MARKER in text,
        "wrong_passphrase_rejected": wrong_passphrase_failed,
    }

    print("--- 判定 ---", flush=True)

    for name, value in checks.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(checks.values())
    print("KEY_AUTH_SMOKE_OK" if ok else "KEY_AUTH_SMOKE_FAILED", flush=True)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
