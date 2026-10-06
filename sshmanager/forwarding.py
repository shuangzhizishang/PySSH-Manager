"""本地端口转发（等价于 ``ssh -L``）。

注意机制：paramiko 的 ``Transport.request_port_forward()`` 是**远端转发**
（让服务器监听端口，把连接送回客户端），并不是 ``ssh -L``。本地转发必须由
本进程在 ``bind:local_port`` 上监听，收到连接后再用 ``direct-tcpip`` 通道
把数据经 SSH 送到 ``remote_host:remote_port``。
"""

from __future__ import annotations

import select
import socket
import threading
from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Signal

from .hosts import Host
from .sshclient import SSHClient


@dataclass
class ForwardRule:
    """一条本地端口转发规则。"""

    bind: str = "127.0.0.1"
    local_port: int = 8080
    remote_host: str = "127.0.0.1"
    remote_port: int = 80
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ForwardRule":
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        rule = cls(**{key: value for key, value in data.items() if key in known})

        try:
            rule.local_port = int(rule.local_port)
            rule.remote_port = int(rule.remote_port)
        except (TypeError, ValueError):
            pass

        return rule

    @property
    def summary(self) -> str:
        return f"{self.bind}:{self.local_port} → {self.remote_host}:{self.remote_port}"


def load_rules(host: Host) -> list[ForwardRule]:
    """从主机配置里读取转发规则（兼容旧数据）。"""

    rules: list[ForwardRule] = []

    for item in getattr(host, "forwards", []) or []:
        if not isinstance(item, dict):
            continue

        try:
            rules.append(ForwardRule.from_dict(item))
        except TypeError:
            continue

    return rules


def save_rules(host: Host, rules: list[ForwardRule]) -> None:
    host.forwards = [rule.to_dict() for rule in rules]


def bridge_socket_to_channel(transport, local_socket: socket.socket, remote_host: str, remote_port: int) -> None:
    """把已建立的本地连接桥接到远端目标，直到任一端关闭。"""

    origin = local_socket.getpeername()[:2]

    try:
        channel = transport.open_channel("direct-tcpip", (remote_host, int(remote_port)), origin)
    except Exception:
        channel = None

    if channel is None:
        try:
            local_socket.close()
        except OSError:
            pass

        return

    local_socket.setblocking(False)
    channel.setblocking(False)

    try:
        while True:
            readable, _, _ = select.select([local_socket, channel], [], [], 1.0)

            if not readable:
                if channel.closed or channel.eof_received:
                    break

                continue

            if local_socket in readable:
                data = local_socket.recv(65536)

                if not data:
                    break

                channel.sendall(data)

            if channel in readable:
                data = channel.recv(65536)

                if not data:
                    break

                local_socket.sendall(data)
    except Exception:
        pass
    finally:
        for resource in (channel, local_socket):
            try:
                resource.close()
            except Exception:
                pass


class ForwardManager(QObject):
    """管理某个主机上的本地端口转发。"""

    #: (规则, 是否成功, 说明)
    state_changed = Signal(object, bool, str)

    def __init__(self, ssh_client: SSHClient, parent=None) -> None:
        super().__init__(parent)

        self.ssh = ssh_client
        self._active: dict[int, ForwardRule] = {}
        self._listeners: dict[int, socket.socket] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._stop = threading.Event()

    # ------------------------------------------------------------------

    def is_active(self, index: int) -> bool:
        return index in self._active

    def active_count(self) -> int:
        return len(self._active)

    def _transport(self):
        if self.ssh.client is None:
            return None

        transport = self.ssh.client.get_transport()

        if transport is None or not transport.is_active():
            return None

        return transport

    # ------------------------------------------------------------------

    def start(self, index: int, rule: ForwardRule) -> bool:
        """在本机监听 ``bind:local_port``，把连接经 SSH 转发到远端目标。"""

        if index in self._active:
            return True

        if self._transport() is None:
            self.state_changed.emit(rule, False, "SSH 连接不可用")
            return False

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            listener.bind((rule.bind, int(rule.local_port)))
            listener.listen(16)
            listener.settimeout(0.5)
        except OSError as error:
            listener.close()
            self.state_changed.emit(rule, False, f"本地监听失败：{error}")
            return False

        self._stop.clear()
        thread = threading.Thread(
            target=self._accept_loop,
            args=(listener, rule),
            daemon=True,
            name=f"forward-{rule.local_port}",
        )

        self._active[index] = rule
        self._listeners[index] = listener
        self._threads[index] = thread
        thread.start()

        self.state_changed.emit(rule, True, f"已开启 {rule.summary}")

        return True

    def _accept_loop(self, listener: socket.socket, rule: ForwardRule) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            transport = self._transport()

            if transport is None:
                try:
                    connection.close()
                except OSError:
                    pass

                continue

            threading.Thread(
                target=bridge_socket_to_channel,
                args=(transport, connection, rule.remote_host, rule.remote_port),
                daemon=True,
            ).start()

    def stop(self, index: int) -> None:
        rule = self._active.pop(index, None)

        if rule is None:
            return

        listener = self._listeners.pop(index, None)

        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

        thread = self._threads.pop(index, None)

        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

        self.state_changed.emit(rule, False, f"已停止 {rule.summary}")

    def stop_all(self) -> None:
        self._stop.set()

        for index in list(self._active):
            self.stop(index)
