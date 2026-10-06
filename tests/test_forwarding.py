"""端口转发的纯单元测试（本地回环，不需要 SSH）。

重点回归：桥接必须用阻塞读 + 超时，不能对 Channel 用 select——
用 select 时「数据已在缓冲区」不一定报可读，会表现为单方向不通。
"""

from __future__ import annotations

import socket
import threading
import time

from sshmanager.forwarding import (
    KIND_DYNAMIC,
    KIND_LOCAL,
    KIND_REMOTE,
    ForwardRule,
    Socks5Handler,
    _half_close,
    _pump,
    bridge_channels,
)


class FakeChannel:
    """最小可用的「类 Channel」对象，用来替代 paramiko 的 Channel。"""

    def __init__(self, incoming: bytes = b"") -> None:
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.closed = False
        self._lock = threading.Lock()

    def recv(self, size: int) -> bytes:
        with self._lock:
            if self.incoming:
                chunk = bytes(self.incoming[:size])
                del self.incoming[:size]
                return chunk

        if self.closed:
            return b""

        # 模拟「暂时没有数据」：抛超时，让 _pump 走 idle 分支再重试
        raise socket.timeout()

    def sendall(self, data: bytes) -> None:
        with self._lock:
            self.outgoing.extend(data)

    def settimeout(self, _timeout) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @property
    def eof_received(self) -> bool:
        return not self.incoming


def test_pump_moves_data_from_channel_to_socket():
    """Channel → socket：即使 select 检测不到，也必须能读到缓冲里的数据。"""

    left = FakeChannel(b"PING")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    connection = socket.create_connection(("127.0.0.1", port))
    peer, _ = server.accept()

    pump = threading.Thread(target=_pump, args=(left, peer), daemon=True)
    pump.start()

    connection.settimeout(3)
    assert connection.recv(64) == b"PING"

    left.close()
    pump.join(timeout=3)
    connection.close()
    peer.close()
    server.close()


def test_bridge_channels_both_directions():
    """双向桥接：两个方向都要通，且结束时不挂起。"""

    left = FakeChannel(b"hello")
    right = FakeChannel(b"world")

    thread = threading.Thread(target=bridge_channels, args=(left, right), daemon=True)
    thread.start()
    thread.join(timeout=8)

    assert not thread.is_alive(), "桥接没有正常结束"
    assert bytes(right.outgoing) == b"hello", f"左→右失败：{right.outgoing!r}"
    assert bytes(left.outgoing) == b"world", f"右→左失败：{left.outgoing!r}"


def test_half_close_on_socket_then_channel():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    connection = socket.create_connection(("127.0.0.1", port))
    peer, _ = server.accept()

    _half_close(peer)  # socket 走 shutdown(SHUT_WR)

    assert connection.recv(10) == b"", "半关闭后对端应看到 EOF"
    assert not peer._closed  # 读端仍可用（shutdown 不是 close）

    peer.close()
    connection.close()
    server.close()

    channel = FakeChannel()
    _half_close(channel)
    assert channel.closed


def test_socks5_negotiation_and_connect_request():
    """SOCKS5 握手 + CONNECT：目标地址端口解析正确，并回 0x00 成功。"""

    class Transport:
        def __init__(self) -> None:
            self.requested = None

        def open_channel(self, kind, destination, origin):
            self.requested = (kind, destination, origin)
            return FakeChannel(b"REMOTE")

    transport = Transport()
    handler = Socks5Handler(transport)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    work = threading.Thread(target=handler.handle, args=(None,), daemon=True)
    # handler.handle 需要真实 socket，这里改为在连接建立后调用
    client = socket.create_connection(("127.0.0.1", port))
    connection, _ = server.accept()
    work = threading.Thread(target=handler.handle, args=(connection,), daemon=True)
    work.start()

    client.sendall(b"\x05\x01\x00")
    assert client.recv(2) == b"\x05\x00"

    # 目标 "example.com" 长度 11（0x0b），端口 80（0x0050）
    client.sendall(b"\x05\x01\x00\x03\x0bexample.com\x00\x50")
    reply = client.recv(10)

    assert reply[0] == 0x05 and reply[1] == 0x00
    assert transport.requested is not None
    kind, destination, _origin = transport.requested
    assert kind == "direct-tcpip"
    assert destination == ("example.com", 80)

    client.close()
    work.join(timeout=5)
    server.close()


def test_socks5_rejects_non_connect_command():
    class Transport:
        def open_channel(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("BIND 命令不应建立通道")

    handler = Socks5Handler(Transport())

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client = socket.create_connection(("127.0.0.1", port))
    connection, _ = server.accept()
    work = threading.Thread(target=handler.handle, args=(connection,), daemon=True)
    work.start()

    client.sendall(b"\x05\x01\x00")
    assert client.recv(2) == b"\x05\x00"

    client.sendall(b"\x05\x02\x00\x01\x7f\x00\x00\x01\x00\x50")
    reply = client.recv(10)

    assert reply[1] == 0x07, "非 CONNECT 命令应返回 0x07"

    client.close()
    work.join(timeout=5)
    server.close()


def test_rule_kinds_and_summaries():
    local = ForwardRule(kind=KIND_LOCAL, local_port=8080, remote_port=80)
    remote = ForwardRule(
        kind=KIND_REMOTE, bind="0.0.0.0", remote_port=2022, remote_host="127.0.0.1", local_port=22
    )
    dynamic = ForwardRule(kind=KIND_DYNAMIC, local_port=1080)

    assert "→" in local.summary
    assert "←" in remote.summary
    assert "SOCKS5" in dynamic.summary

    # 未知类型回退到本地转发
    assert ForwardRule(kind="???").kind == KIND_LOCAL
