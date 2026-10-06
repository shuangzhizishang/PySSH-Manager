"""paramiko 封装：认证、known_hosts 校验、PTY、SFTP、端口转发。"""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import paramiko

from . import settings
from .hosts import Host


class HostKeyMismatch(paramiko.SSHException):
    """服务器指纹与 known_hosts 不一致，可能遭遇中间人攻击。"""


class _Policy(paramiko.MissingHostKeyPolicy):
    """未知主机：给出带指纹的明确错误，而不是静默接受。"""

    def missing_host_key(self, client, hostname, key):
        fingerprint = key.get_fingerprint().hex()
        raise HostKeyMismatch(
            f"未知主机 {hostname}，指纹 {key.get_name()} {':'.join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2))}\n"
            f"确认无误后可在「连接设置」中关闭指纹校验，或把该主机写入 known_hosts。"
        )


def save_host_key(
    hostname: str,
    port: int = 22,
    known_hosts_path: Path | None = None,
    timeout: float = 10.0,
) -> str:
    """读取服务器公钥并写入本应用自己的 known_hosts，返回指纹。

    用于用户确认「信任此服务器」之后。
    """

    path = Path(known_hosts_path) if known_hosts_path else settings.legacy_known_hosts_file()
    path.parent.mkdir(parents=True, exist_ok=True)

    transport = paramiko.Transport((hostname, int(port)))
    transport.start_client(timeout=timeout)

    try:
        key = transport.get_remote_server_key()
        fingerprint = key.get_fingerprint().hex()
        formatted = ":".join(fingerprint[index : index + 2] for index in range(0, len(fingerprint), 2))
        host_entry = hostname if int(port) == 22 else f"[{hostname}]:{port}"

        line = f"{host_entry} {key.get_name()} {key.get_base64()}\n"

        existing = path.read_text(encoding="utf-8") if path.exists() else ""

        if line not in existing:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    finally:
        transport.close()

    return formatted


class SSHClient:
    """单个 SSH 连接。所有方法都可在后台线程中调用。"""

    def __init__(self, host: Host | None = None) -> None:
        self.host = host
        self.client: paramiko.SSHClient | None = None
        self.channel: paramiko.Channel | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def connect(
        self,
        hostname: str,
        port: int = 22,
        username: str = "root",
        password: str | None = None,
        key_path: str | None = None,
        passphrase: str | None = None,
        verify_host_key: bool = True,
        known_hosts_path: Path | None = None,
        term_type: str = "xterm-256color",
        width: int = 120,
        height: int = 32,
        timeout: float = 10.0,
        auth_timeout: float = 45.0,
        jump: "SSHClient | None" = None,
    ) -> None:
        """建立连接并打开带 PTY 的交互式 Shell。

        ``timeout`` 是 TCP/握手超时，``auth_timeout`` 是认证阶段超时：
        部分老服务器（如 CentOS 7 的旧版 OpenSSH）密钥交换 + 认证要好几秒，
        认证超时设置过短会误报 "Authentication timeout"。
        """

        client = paramiko.SSHClient()

        if verify_host_key:
            path = known_hosts_path or settings.legacy_known_hosts_file()
            path = Path(path)

            if not path.exists():
                # paramiko 在文件不存在时会直接抛异常，这里先建一个空文件
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            client.load_host_keys(str(path))
            client.set_missing_host_key_policy(_Policy())
        else:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs: dict = {
            "hostname": hostname,
            "port": int(port),
            "username": username,
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": auth_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }

        if key_path:
            kwargs["key_filename"] = os.path.expanduser(key_path)

            if passphrase:
                kwargs["passphrase"] = passphrase
        elif password:
            kwargs["password"] = password

        if jump is not None and jump.client is not None and jump.client.get_transport() is not None:
            transport = jump.client.get_transport()
            channel = transport.open_channel(
                "direct-tcpip",
                (hostname, int(port)),
                ("127.0.0.1", 0),
                timeout=timeout,
            )
            kwargs["sock"] = channel

        client.connect(**kwargs)

        with self._lock:
            self.client = client
            self.channel = client.invoke_shell(
                term=term_type,
                width=max(2, int(width)),
                height=max(2, int(height)),
            )
            self.channel.settimeout(None)

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            if self.client is None:
                return False
            transport = self.client.get_transport()
            return bool(transport and transport.is_active())

    def send(self, data: str | bytes) -> None:
        with self._lock:
            channel = self.channel

        if channel is None:
            return

        payload = data.encode("utf-8") if isinstance(data, str) else data

        try:
            channel.sendall(payload)
        except (OSError, paramiko.SSHException):
            pass

    def recv(self, size: int = 65536) -> bytes:
        """阻塞读取；连接关闭时返回 b""。"""

        with self._lock:
            channel = self.channel

        if channel is None:
            return b""

        try:
            data = channel.recv(size)
        except socket.timeout:
            return b""
        except (OSError, paramiko.SSHException):
            return b""

        if not data:
            return b""

        return data

    def resize(self, width: int, height: int) -> None:
        with self._lock:
            channel = self.channel

        if channel is None:
            return

        try:
            channel.resize_pty(width=max(2, int(width)), height=max(2, int(height)))
        except (OSError, paramiko.SSHException):
            pass

    def close(self) -> None:
        with self._lock:
            channel, client = self.channel, self.client
            self.channel = None
            self.client = None

        for resource in (channel, client):
            if resource is None:
                continue

            try:
                resource.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # SFTP / 端口转发
    # ------------------------------------------------------------------

    def open_sftp(self) -> paramiko.SFTPClient:
        if self.client is None:
            raise RuntimeError("尚未连接")

        return self.client.open_sftp()

    def forward_local(self, local_port: int, remote_host: str, remote_port: int, bind: str = "127.0.0.1"):
        """本地端口转发：本地端口 -> SSH 服务器可达的 remote_host:remote_port。"""

        if self.client is None:
            raise RuntimeError("尚未连接")

        transport = self.client.get_transport()

        if transport is None:
            raise RuntimeError("SSH 传输层不可用")

        return transport.request_port_forward(bind, local_port)
