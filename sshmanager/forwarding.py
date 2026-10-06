"""端口转发：本地（``ssh -L``）、远端（``ssh -R``）、动态（SOCKS5 代理）。

机制说明（容易搞错，先写在这里）:

- **本地转发**（local，``ssh -L``）：本进程监听 ``bind:local_port``，
  收到连接后用 ``direct-tcpip`` 通道把数据送到 ``remote_host:remote_port``。
  不能用 ``Transport.request_port_forward()``，那是远端转发。
- **远端转发**（remote，``ssh -R``）：让 SSH 服务器监听 ``bind:remote_port``，
  连接到来时 paramiko 把通道放进 accept 队列，客户端再连到
  ``local_host:local_port``。
- **动态转发**（dynamic，``ssh -D``）：本进程起一个 SOCKS5 服务，
  由客户端在握手时告诉我们要连的 ``host:port``，再经 ``direct-tcpip`` 转发。

实现上有两个坑，改动时务必保留：

1. **不要对 paramiko 的 Channel 用 select()**。Channel 的数据可能已经在它的
   内部缓冲区里，此时 select 不一定报可读，会表现为「单方向不通」。统一改成
   阻塞读 + 超时（:func:`_pump`）。
2. **远端转发要用 ``Transport.accept()`` 取连接**，不要给
   ``request_port_forward()`` 传自定义 handler 后再自己 select 通道。
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import asdict, dataclass

from PySide6.QtCore import QObject, Signal

from .hosts import Host
from .sshclient import SSHClient

KIND_LOCAL = "local"
KIND_REMOTE = "remote"
KIND_DYNAMIC = "dynamic"

KIND_LABELS = {
    KIND_LOCAL: "本地转发 (ssh -L)",
    KIND_REMOTE: "远端转发 (ssh -R)",
    KIND_DYNAMIC: "动态转发 SOCKS5 (ssh -D)",
}

READ_TIMEOUT = 30.0
IDLE_LIMIT = 20  # 连续多少次读超时后放弃（约 READ_TIMEOUT * IDLE_LIMIT 秒的无流量）


@dataclass
class ForwardRule:
    """一条转发规则。

    ``kind`` 决定哪些字段有意义：

    - ``local``：``bind`` ``local_port`` → ``remote_host`` ``remote_port``
    - ``remote``：服务器 ``bind`` ``remote_port`` ← 本机 ``remote_host`` ``local_port``
    - ``dynamic``：本机 ``bind`` ``local_port`` 作为 SOCKS5 代理
    """

    kind: str = KIND_LOCAL
    bind: str = "127.0.0.1"
    local_port: int = 8080
    remote_host: str = "127.0.0.1"
    remote_port: int = 80
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KIND_LABELS:
            self.kind = KIND_LOCAL

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
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def summary(self) -> str:
        if self.kind == KIND_REMOTE:
            return f"远端 {self.bind}:{self.remote_port} ← 本机 {self.remote_host}:{self.local_port}"

        if self.kind == KIND_DYNAMIC:
            return f"SOCKS5 {self.bind}:{self.local_port}"

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


# ----------------------------------------------------------------------
# 通道桥接（阻塞读 + 超时，不用 select 轮询 Channel）
# ----------------------------------------------------------------------

def _half_close(resource) -> None:
    """单向结束：socket 走 shutdown 半关闭，Channel 直接关闭。"""

    if hasattr(resource, "shutdown"):
        try:
            resource.shutdown(socket.SHUT_WR)
            return
        except OSError:
            pass

    try:
        resource.close()
    except Exception:
        pass


def _pump(source, target, stop: threading.Event | None = None) -> None:
    """把 source 的数据搬到 target，直到任一端结束。"""

    idle = 0

    while stop is None or not stop.is_set():
        try:
            data = source.recv(65536)
        except socket.timeout:
            idle += 1

            if stop is not None and stop.is_set():
                break

            if idle >= IDLE_LIMIT:
                break

            continue
        except OSError:
            break

        idle = 0

        if not data:
            break

        try:
            target.sendall(data)
        except Exception:
            break

    _half_close(target)


def bridge_channels(left, right, stop: threading.Event | None = None) -> None:
    """双向桥接两个「类 socket」对象（本地 socket 或 paramiko Channel）。"""

    for resource in (left, right):
        try:
            resource.settimeout(READ_TIMEOUT)
        except Exception:
            pass

    back = threading.Thread(target=_pump, args=(right, left, stop), daemon=True)
    back.start()
    _pump(left, right, stop)

    back.join(timeout=5)

    for resource in (left, right):
        try:
            resource.close()
        except Exception:
            pass


def bridge_socket_to_channel(transport, local_socket: socket.socket, remote_host: str, remote_port: int) -> None:
    """把已建立的本地 socket 接到一条 ``direct-tcpip`` 远端通道。"""

    try:
        origin = local_socket.getpeername()[:2]
    except OSError:
        origin = ("127.0.0.1", 0)

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

    bridge_channels(local_socket, channel)


# ----------------------------------------------------------------------
# SOCKS5（动态转发）
# ----------------------------------------------------------------------

class Socks5Handler:
    """极简 SOCKS5 服务端：只支持无认证的 CONNECT 命令。"""

    def __init__(self, transport) -> None:
        self.transport = transport

    def handle(self, connection: socket.socket) -> None:
        try:
            if not self._negotiate(connection):
                return

            target = self._read_request(connection)

            if target is None:
                return

            host, port = target

            try:
                channel = self.transport.open_channel(
                    "direct-tcpip",
                    (host, port),
                    connection.getpeername()[:2],
                )
            except Exception:
                self._reply(connection, 0x04)  # host unreachable
                return

            if channel is None:
                self._reply(connection, 0x04)
                return

            self._reply(connection, 0x00)
            bridge_channels(connection, channel)
        except Exception:
            try:
                connection.close()
            except OSError:
                pass

    # ------------------------------------------------------------------

    @staticmethod
    def _negotiate(connection: socket.socket) -> bool:
        header = connection.recv(2)

        if len(header) < 2 or header[0] != 0x05:
            return False

        methods = connection.recv(header[1])

        if 0x00 not in methods:
            connection.sendall(b"\x05\xff")
            return False

        connection.sendall(b"\x05\x00")

        return True

    @staticmethod
    def _read_request(connection: socket.socket):
        header = connection.recv(4)

        if len(header) < 4 or header[0] != 0x05:
            return None

        if header[1] != 0x01:  # 只支持 CONNECT
            connection.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return None

        address_type = header[3]

        if address_type == 0x01:
            host = socket.inet_ntoa(connection.recv(4))
        elif address_type == 0x03:
            length = connection.recv(1)

            if not length:
                return None

            host = connection.recv(length[0]).decode("utf-8", errors="replace")
        elif address_type == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, connection.recv(16))
        else:
            return None

        raw_port = connection.recv(2)

        if len(raw_port) < 2:
            return None

        return host, struct.unpack("!H", raw_port)[0]

    @staticmethod
    def _reply(connection: socket.socket, code: int) -> None:
        try:
            connection.sendall(b"\x05" + bytes([code]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass


# ----------------------------------------------------------------------
# 管理器
# ----------------------------------------------------------------------

class ForwardManager(QObject):
    """管理某个主机上的全部转发（本地 / 远端 / 动态）。"""

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
        if self.ssh is None:
            return None

        transport = self.ssh.transport

        if transport is None or not transport.is_active():
            return None

        return transport

    # ------------------------------------------------------------------

    def start(self, index: int, rule: ForwardRule) -> bool:
        if index in self._active:
            return True

        if self._transport() is None:
            self.state_changed.emit(rule, False, "SSH 连接不可用")
            return False

        if rule.kind == KIND_REMOTE:
            return self._start_remote(index, rule)

        if rule.kind == KIND_DYNAMIC:
            return self._start_dynamic(index, rule)

        return self._start_local(index, rule)

    # ------------------------------------------------------------------
    # 本地转发（ssh -L）
    # ------------------------------------------------------------------

    def _start_local(self, index: int, rule: ForwardRule) -> bool:
        listener = self._bind(rule.bind, int(rule.local_port))

        if listener is None:
            self.state_changed.emit(rule, False, f"本地监听失败：{rule.bind}:{rule.local_port}")
            return False

        self._stop.clear()
        thread = threading.Thread(
            target=self._accept_loop,
            args=(listener, rule, False),
            daemon=True,
            name=f"forward-local-{rule.local_port}",
        )

        self._register(index, rule, listener, thread)
        self.state_changed.emit(rule, True, f"已开启 {rule.summary}")

        return True

    # ------------------------------------------------------------------
    # 动态转发（ssh -D，SOCKS5）
    # ------------------------------------------------------------------

    def _start_dynamic(self, index: int, rule: ForwardRule) -> bool:
        listener = self._bind(rule.bind, int(rule.local_port))

        if listener is None:
            self.state_changed.emit(rule, False, f"SOCKS5 监听失败：{rule.bind}:{rule.local_port}")
            return False

        self._stop.clear()
        thread = threading.Thread(
            target=self._accept_loop,
            args=(listener, rule, True),
            daemon=True,
            name=f"forward-socks-{rule.local_port}",
        )

        self._register(index, rule, listener, thread)
        self.state_changed.emit(rule, True, f"已开启 {rule.summary}")

        return True

    # ------------------------------------------------------------------
    # 远端转发（ssh -R）
    # ------------------------------------------------------------------

    def _start_remote(self, index: int, rule: ForwardRule) -> bool:
        transport = self._transport()

        # 用 handler 形式：回调能拿到服务器实际监听的地址端口，便于对应到规则。
        # 注意桥接必须用阻塞读（见 _pump 的说明），不能对 Channel 用 select。
        def handler(channel, origin, server, bound_rule=rule) -> None:  # noqa: ANN001 - paramiko 回调签名
            threading.Thread(
                target=self._serve_remote,
                args=(channel, bound_rule),
                daemon=True,
            ).start()

        try:
            transport.request_port_forward(rule.bind, int(rule.remote_port), handler)
        except Exception as error:  # noqa: BLE001
            self.state_changed.emit(rule, False, f"远端转发开启失败：{error}")
            return False

        self._stop.clear()

        self._active[index] = rule
        self.state_changed.emit(rule, True, f"已开启 {rule.summary}")

        return True

    def _serve_remote(self, channel, rule: ForwardRule) -> None:
        """服务器送来一条连接：连到本机 ``remote_host:local_port`` 再对接。"""

        try:
            local_socket = socket.create_connection((rule.remote_host, int(rule.local_port)), timeout=8)
        except OSError as error:
            self.state_changed.emit(rule, False, f"连本机 {rule.remote_host}:{rule.local_port} 失败：{error}")
            channel.close()
            return

        bridge_channels(channel, local_socket, self._stop)

    # ------------------------------------------------------------------
    # 公共部分
    # ------------------------------------------------------------------

    @staticmethod
    def _bind(address: str, port: int):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            listener.bind((address, port))
            listener.listen(16)
            listener.settimeout(0.5)
        except OSError:
            listener.close()
            return None

        return listener

    def _accept_loop(self, listener: socket.socket, rule: ForwardRule, socks: bool) -> None:
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

            if socks:
                threading.Thread(
                    target=Socks5Handler(transport).handle,
                    args=(connection,),
                    daemon=True,
                ).start()
            else:
                threading.Thread(
                    target=bridge_socket_to_channel,
                    args=(transport, connection, rule.remote_host, rule.remote_port),
                    daemon=True,
                ).start()

    def _register(self, index: int, rule: ForwardRule, listener, thread) -> None:
        self._active[index] = rule
        self._listeners[index] = listener
        self._threads[index] = thread
        thread.start()

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

        if rule.kind == KIND_REMOTE:
            transport = self._transport()

            if transport is not None:
                try:
                    transport.cancel_port_forward(rule.bind, int(rule.remote_port))
                except Exception:
                    pass

        self.state_changed.emit(rule, False, f"已停止 {rule.summary}")

    def stop_all(self) -> None:
        self._stop.set()

        for index in list(self._active):
            self.stop(index)
