"""新增功能的单元测试：同步输入广播、分屏上限、目录过滤。

不需要网络；用假的会话对象验证"输入会被转发且不会回环"。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sshmanager.sftpcore import match_name  # noqa: E402
from sshmanager.ui.session_tab import TerminalTab  # noqa: E402
from sshmanager.ui.split_tab import SplitTerminalTab  # noqa: E402

_app: QApplication | None = None


def _application() -> QApplication:
    global _app

    if _app is None:
        _app = QApplication.instance() or QApplication([])

    return _app


class FakeSession:
    """记录收到的数据，用来观察输入有没有被正确转发。"""

    def __init__(self) -> None:
        self.received: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.received.append(data)

    def set_terminal_size(self, columns: int, rows: int) -> None:
        pass


@pytest.fixture()
def tabs():
    _application()

    first = TerminalTab()
    second = TerminalTab()
    first.session = FakeSession()
    second.session = FakeSession()

    return first, second


# ----------------------------------------------------------------------
# 同步输入
# ----------------------------------------------------------------------

def test_user_input_reaches_own_session_and_emits_signal(tabs):
    first, _second = tabs
    seen: list[bytes] = []
    first.input_generated.connect(seen.append)

    first._on_input(b"pwd\r")

    assert first.session.received == [b"pwd\r"]
    assert seen == [b"pwd\r"], "主窗口需要收到通知才能广播"


def test_write_input_does_not_emit_again(tabs):
    """广播转发的数据不能再触发 input_generated，否则会来回转发（回环）。"""

    first, _second = tabs
    seen: list[bytes] = []
    first.input_generated.connect(seen.append)

    first.write_input(b"pwd\r")

    assert first.session.received == [b"pwd\r"]
    assert seen == [], "转发的数据不应再次广播"


def test_broadcast_fanout_with_stub_panes():
    """MainWindow._on_pane_input：开启同步时发给其它远端会话，且跳过自己与本地终端。"""

    from sshmanager.credentials import CredentialStore
    from sshmanager.hosts import HostStore
    from sshmanager.ui import AppContext, MainWindow

    _application()

    window = MainWindow(AppContext(hosts=HostStore(), credentials=CredentialStore()))

    class Pane:
        def __init__(self, local: bool = False) -> None:
            self.is_local = local
            self.received: list[bytes] = []

        def write_input(self, data: bytes) -> None:
            self.received.append(data)

    source = Pane()
    other = Pane()
    local = Pane(local=True)

    window._remote_panes = lambda: [source, other, local]  # type: ignore[assignment]

    window.sync_input = False
    window._on_pane_input(source, b"x")
    assert other.received == [], "未开启同步时不应广播"

    window.sync_input = True
    window._on_pane_input(source, b"pwd\r")
    assert other.received == [b"pwd\r"], "其它远端会话应收到输入"
    assert source.received == [], "不应把自己也算进去"

    window._on_pane_input(local, b"y")
    assert other.received == [b"pwd\r"], "本地终端不该往外广播"

    window.shutdown()


# ----------------------------------------------------------------------
# 分屏上限
# ----------------------------------------------------------------------

def test_split_max_panes_is_two():
    assert SplitTerminalTab.MAX_PANES == 2


def test_split_stops_at_limit(tabs):
    """已有一个面板时只允许再分一个，第三次返回 None。"""

    _application()

    from sshmanager.hosts import Host

    split = SplitTerminalTab(Host(label="t", hostname="127.0.0.1", username="root"))
    assert len(split.panes) == 1

    from PySide6.QtCore import Qt

    assert split.add_pane(activate=False) is not None
    assert len(split.panes) == 2
    assert split.split(Qt.Orientation.Horizontal) is None, "超过上限应返回 None"
    assert len(split.panes) == 2

    split.close()  # 面板里的 SessionTab 会起连接线程，测完要收干净


# ----------------------------------------------------------------------
# 双主机分屏（左边一台虚拟机、右边另一台）
# ----------------------------------------------------------------------

def test_split_tab_accepts_different_hosts_per_pane():
    _application()

    from sshmanager.hosts import Host

    left = Host(label="left", hostname="10.0.0.1", username="root")
    right = Host(label="right", hostname="10.0.0.2", username="root")

    split = SplitTerminalTab(left, first_pane=False)
    split.add_pane(host=left, activate=False)
    split.add_pane(host=right, activate=False)

    try:
        assert len(split.panes) == 2
        assert split.panes[0].host.id == left.id
        assert split.panes[1].host.id == right.id
        assert [host.id for host in split.hosts] == [left.id, right.id]
        assert split.pane_for_host(right.id) is split.panes[1]
        # 两台不同主机 → 标题显示成 A ⇄ B，而不是某一侧的远端标题
        assert split.display_name == "left ⇄ right"
    finally:
        split.close()


def test_display_name_uses_remote_title_only_for_same_host():
    _application()

    from sshmanager.hosts import Host

    host = Host(label="same", hostname="10.0.0.9", username="root")
    split = SplitTerminalTab(host, first_pane=False)
    split.add_pane(host=host, activate=False)
    split.add_pane(host=host, activate=False)

    try:
        assert len(split.panes) == 2
        split.panes[0].remote_title = "root@same:~"
        split.panes[1].remote_title = "root@same:~"
        assert split.display_name == "root@same:~", "同一台主机时用远端标题更直观"
    finally:
        split.close()


def test_open_split_hosts_puts_two_hosts_side_by_side():
    """主窗口入口：两台主机 → 一个标签两个面板，各自连不同主机。"""

    from sshmanager.credentials import CredentialStore
    from sshmanager.hosts import Host, HostStore
    from sshmanager.ui import AppContext, MainWindow

    from PySide6.QtCore import Qt

    _application()

    hosts = HostStore()
    credentials = CredentialStore()
    context = AppContext(hosts=hosts, credentials=credentials)

    left = Host(label="left", hostname="127.0.0.1", port=1, username="root")
    right = Host(label="right", hostname="127.0.0.2", port=1, username="root")
    hosts.add(left)
    hosts.add(right)
    credentials.set(left.id, "x")
    credentials.set(right.id, "x")

    window = MainWindow(context)
    window.open_split_hosts(left, right, Qt.Orientation.Horizontal)
    _application().processEvents()

    try:
        split = window.tabs_widget.currentWidget()
        assert isinstance(split, SplitTerminalTab)
        assert [pane.host.id for pane in split.panes] == [left.id, right.id]
        assert split.splitter.orientation() == Qt.Orientation.Horizontal
        assert window.tabs_widget.count() == 1

        title = window.tabs_widget.tabText(window.tabs_widget.currentIndex())
        assert "left" in title and "right" in title, f"标题应体现两台主机：{title}"
    finally:
        window.shutdown()


# ----------------------------------------------------------------------
# 目录过滤
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "pattern", "expected"),
    [
        ("mysql-community-server.rpm", "", True),
        ("mysql-community-server.rpm", "mysql", True),
        ("mysql-community-server.rpm", "MYSQL", True),
        ("mysql-community-server.rpm", "mysql rpm", True),
        ("mysql-community-server.rpm", "mysql zip", False),
        ("libssl.so.10", "*.so", False),      # 不做通配符，按子串匹配
        ("libssl.so.10", "so.1", True),
        ("readme.txt", "   ", True),          # 只有空格视为不过滤
    ],
)
def test_match_name(name, pattern, expected):
    assert match_name(name, pattern) is expected


def test_match_name_handles_chinese():
    assert match_name("构建日志.txt", "构建")
    assert not match_name("构建日志.txt", "构建 缺失")


# ----------------------------------------------------------------------
# SFTP 远端列表：过滤与截断（离屏跑，不需要连接）
# ----------------------------------------------------------------------

def _make_panel():
    _application()

    from sshmanager.hosts import Host
    from sshmanager.ui.sftp_panel import SftpPanel

    # 指向本机不可达端口：worker 会失败退出，但我们只用其列表逻辑
    return SftpPanel(Host(label="t", hostname="127.0.0.1", port=1, username="root"))


def test_remote_listing_fills_table_and_maps_selection():
    from sshmanager.sftpcore import RemoteEntry

    panel = _make_panel()
    entries = [
        RemoteEntry(name="alpha.txt", path="/root/alpha.txt", is_dir=False, size=10),
        RemoteEntry(name="bin", path="/root/bin", is_dir=True, size=0),
        RemoteEntry(name="beta.log", path="/root/beta.log", is_dir=False, size=20),
    ]

    try:
        panel._on_listing("/root", entries)

        assert panel._visible_entries == entries
        assert panel.remote_table.rowCount() == 3
        assert panel.remote_table.item(0, 0).text().endswith("alpha.txt")
    finally:
        panel.close()


def test_remote_filter_narrows_rows_and_keeps_mapping():
    from sshmanager.sftpcore import RemoteEntry

    panel = _make_panel()
    entries = [
        RemoteEntry(name="mysql-server.rpm", path="/rpm/mysql-server.rpm", is_dir=False, size=1),
        RemoteEntry(name="redis-server.rpm", path="/rpm/redis-server.rpm", is_dir=False, size=1),
        RemoteEntry(name="mysql-client.rpm", path="/rpm/mysql-client.rpm", is_dir=False, size=1),
    ]

    try:
        panel._on_listing("/rpm", entries)
        assert panel.remote_table.rowCount() == 3

        panel.remote_filter.setText("mysql")
        assert panel.remote_table.rowCount() == 2
        assert [entry.name for entry in panel._visible_entries] == [
            "mysql-server.rpm",
            "mysql-client.rpm",
        ]

        panel.remote_filter.setText("MySQL server")
        assert [entry.name for entry in panel._visible_entries] == ["mysql-server.rpm"]

        panel.remote_filter.setText("")
        assert panel.remote_table.rowCount() == 3
    finally:
        panel.close()


def test_huge_directory_is_capped_with_hint():
    from sshmanager.sftpcore import RemoteEntry

    panel = _make_panel()
    total = panel.MAX_TABLE_ROWS + 200
    entries = [
        RemoteEntry(name=f"file-{index}.so", path=f"/lib/file-{index}.so", is_dir=False, size=1)
        for index in range(total)
    ]

    try:
        panel._on_listing("/lib", entries)

        assert len(panel._remote_entries) == total
        assert panel.remote_table.rowCount() == panel.MAX_TABLE_ROWS, "表格行数应被截断"
        assert "仅显示前" in panel.status_label.text(), "应提示被截断"

        # 过滤后能拿到后面的条目（说明数据没丢，只是没全塞进表格）
        panel.remote_filter.setText(f"file-{total - 1}.")
        assert [entry.name for entry in panel._visible_entries] == [f"file-{total - 1}.so"]
    finally:
        panel.close()
