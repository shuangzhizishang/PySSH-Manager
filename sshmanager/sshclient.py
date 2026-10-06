"""paramiko 封装：认证、known_hosts 校验、PTY、SFTP。

这里**不用** ``paramiko.SSHClient.connect()``，而是自己持有 socket 与
``Transport``。原因：``SSHClient.close()`` 在传输层还没创建时会直接返回，
一旦用户在「正在连接/认证」阶段关闭窗口，那条会话线程就会一直卡在
``Transport.start_client()`` / 认证等待里，进程退出阶段会崩溃
（实测崩溃栈在 paramiko/packet.py 的 recv 上）。自己管 socket 就能在任意
阶段可靠打断。

认证沿用 paramiko 的 ``Transport.auth_*``，因此 ``known_hosts`` 策略、
``save_host_key`` 等能力保持不变。
"""

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


def load_private_key(key_path: str, passphrase: str | None):
    """按密钥类型逐个尝试加载私钥。

    不能直接用 ``PKey.from_path``：它把 str 类型的 passphrase 原样交给
    cryptography，会抛 ``TypeError: password must be bytes``；
    ``PKey.from_private_key_file`` 在抽象基类上也不可用（paramiko 文档明确说明
    "useless on the abstract PKey class"）。所以这里枚举具体类型的加载器，
    并额外兼容自定义/未来类型。
    """

    path = os.path.expanduser(key_path)
    classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    dss = getattr(paramiko, "DSSKey", None)

    if dss is not None:  # 老版本 paramiko 才有
        classes.append(dss)

    last_error: Exception | None = None

    for key_class in classes:
        try:
            return key_class.from_private_key_file(path, password=passphrase)
        except Exception as error:  # noqa: BLE001 - 类型不匹配就试下一个
            last_error = error

    # 兜底：自定义密钥类型可以通过 PKey.from_path 加载
    try:
        return paramiko.PKey.from_path(path, passphrase.encode("utf-8") if passphrase else None)
    except Exception as error:  # noqa: BLE001
        last_error = error

    raise last_error if last_error is not None else paramiko.SSHException("无法加载私钥")


class SSHClient:
    """单个 SSH 连接。所有方法都可在后台线程中调用。"""

    def __init__(self, host: Host | None = None) -> None:
        self.host = host
        self.client: paramiko.SSHClient | None = None
        self.transport: paramiko.Transport | None = None
        self.channel: paramiko.Channel | None = None
        self._sock: socket.socket | None = None
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
        channel_timeout: float = 1.0,
        jump: "SSHClient | None" = None,
    ) -> None:
        """建立连接并打开带 PTY 的交互式 Shell。

        - ``timeout``：TCP/握手超时
        - ``auth_timeout``：认证阶段超时（老服务器密钥交换慢，给宽一些）
        - ``channel_timeout``：交互通道读超时，小一点让读取循环能定期检查退出标志
        """

        # 1) 建底层连接：跳板机则走 direct-tcpip，否则直连
        jump_transport = None

        if jump is not None:
            jump_transport = jump.transport

        if jump_transport is not None and jump_transport.is_active():
            sock = jump_transport.open_channel(
                "direct-tcpip",
                (hostname, int(port)),
                ("127.0.0.1", 0),
                timeout=timeout,
            )
        else:
            sock = socket.create_connection((hostname, int(port)), timeout=timeout)

        with self._lock:
            self._sock = sock

        transport = paramiko.Transport(sock)

        with self._lock:
            self.transport = transport

        # 2) 主机密钥校验：未知主机交给策略，指纹变化直接报错
        client = paramiko.SSHClient()
        known_keys = None

        if verify_host_key:
            path = Path(known_hosts_path) if known_hosts_path else settings.legacy_known_hosts_file()

            if not path.exists():
                # paramiko 读不存在的文件会抛异常，先建一个空的
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")

            client.load_host_keys(str(path))
            known_keys = client.get_host_keys()

        try:
            transport.banner_timeout = timeout
            transport.auth_timeout = auth_timeout
            transport.start_client(timeout=auth_timeout)
        except BaseException:
            self.close()
            raise

        remote_key = transport.get_remote_server_key()

        if verify_host_key:
            entries = known_keys.lookup(hostname) if known_keys is not None else None

            if entries:
                if remote_key.get_name() not in entries:
                    self.close()
                    raise HostKeyMismatch(
                        f"服务器 {hostname} 提供的密钥类型 {remote_key.get_name()} 与 known_hosts 记录不一致，已中断连接。"
                    )

                if entries[remote_key.get_name()] != remote_key:
                    self.close()
                    raise HostKeyMismatch(
                        f"服务器 {hostname} 的指纹与 known_hosts 记录不一致，可能被中间人劫持，已中断连接。"
                    )
            else:
                try:
                    _Policy().missing_host_key(client, hostname, remote_key)
                except BaseException:
                    self.close()
                    raise

        # 3) 认证
        try:
            if key_path:
                key = load_private_key(key_path, passphrase)
                transport.auth_publickey(username, key)
            else:
                # Transport.auth_password 只接受 bytes
                transport.auth_password(username, (password or "").encode("utf-8"))
        except BaseException:
            self.close()
            raise

        if not transport.is_authenticated():
            self.close()
            raise paramiko.AuthenticationException(f"认证未通过：{username}@{hostname}:{port}")

        # 4) 交互通道
        try:
            channel = transport.open_session(timeout=timeout)
            channel.get_pty(
                term=term_type,
                width=max(2, int(width)),
                height=max(2, int(height)),
            )
            channel.invoke_shell()
        except BaseException:
            self.close()
            raise

        channel.settimeout(channel_timeout)

        with self._lock:
            self.client = client
            self.channel = channel

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            transport = self.transport

        return bool(transport is not None and transport.is_active() and transport.is_authenticated())

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
        """读取数据；超时或连接关闭时返回 b""。"""

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

        return data or b""

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
            channel, transport, client, sock = self.channel, self.transport, self.client, self._sock
            self.channel = None
            self.transport = None
            self.client = None
            self._sock = None

        # 顺序：通道 → 传输层（停掉 paramiko 读线程）→ 客户端 → 原始 socket
        for resource in (channel, transport, client, sock):
            if resource is None:
                continue

            try:
                resource.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # SFTP / 一次性命令
    # ------------------------------------------------------------------

    def open_sftp(self) -> paramiko.SFTPClient:
        transport = self._require_transport()

        return transport.open_sftp_client()

    def exec_command(self, command: str, timeout: float = 15.0):
        """在独立通道上跑一条命令，返回 ``(stdin, stdout, stderr)``。

        用于 SFTP 之外的一次性操作（例如端口转发的探测命令）。
        """

        transport = self._require_transport()
        channel = transport.open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.exec_command(command)

        return channel.makefile_stdin("wb"), channel.makefile("rb"), channel.makefile_stderr("rb")

    def _require_transport(self) -> paramiko.Transport:
        with self._lock:
            transport = self.transport

        if transport is None or not transport.is_active():
            raise RuntimeError("尚未连接")

        return transport

    def is_connected(self) -> bool:
        """传输层存在且已完成认证。"""

        with self._lock:
            transport = self.transport

        return bool(transport is not None and transport.is_active() and transport.is_authenticated())

    # 端口转发见 sshmanager.forwarding.ForwardManager：
    # 本地转发（ssh -L）需要自己在本机监听，不能用 Transport.request_port_forward
    # （那是远端转发 ssh -R）。
