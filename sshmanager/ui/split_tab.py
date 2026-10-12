"""分屏终端标签：一个标签里放多个终端画面。

每个分屏面板都是独立的会话（各自一条 SSH 连接），与 Termius 的分屏行为一致：
面板数量、方向与每个画面的终端尺寸都由布局自动协商。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from ..hosts import Host
from ..sshclient import SSHClient
from .session_tab import SessionTab


class SplitTerminalTab(QWidget):
    """把多个终端面板并排/堆叠在同一个标签里。

    每个面板可以有**自己的主机**（例如左边 192.168.206.130、右边 192.168.206.131），
    这样能把多台虚拟机摆在一起对比着配环境；也可以两个面板连同一台主机。
    """

    title_changed = Signal(str)
    closed = Signal()
    failed = Signal(str)

    #: 某个面板产生了输入（面板, 数据），主窗口据此做同步广播
    pane_input = Signal(object, object)

    #: 面板集合变化（新增/关闭/换了主机）→ 主窗口刷新标签标题与侧边栏计数
    panes_changed = Signal()

    #: 最多两个面板（左右或上下各两个；再多屏幕太挤，且会话数翻倍不好管）
    MAX_PANES = 2

    def __init__(
        self,
        host: Host,
        credential: str | None = None,
        jump: SSHClient | None = None,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent=None,
        first_pane: bool = True,
    ) -> None:
        super().__init__(parent)

        #: 第一个面板的主机（也代表"这个标签属于哪台主机"）
        self.host = host
        self.credential = credential
        self.jump = jump
        self._closing = False

        self.splitter = QSplitter(orientation)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.splitter)

        if first_pane:
            self.add_pane(host=host, credential=credential, jump=jump, activate=False)

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        """标签名：两个面板是不同主机时显示成 「A ⇄ B」。

        多主机时不采用远端 OSC 标题——否则标题会被某一侧覆盖成 `root@xxx:~`，
        看不出这一栏到底是哪台虚拟机。
        """

        panes = self.panes
        same_host = len({pane.host.id for pane in panes if pane.host is not None}) <= 1
        names: list[str] = []

        for pane in panes:
            name = pane.host.display_name if pane.host is not None else "本地"

            if same_host:
                name = getattr(pane, "remote_title", "") or name

            if name not in names:
                names.append(name)

        if not names:
            return "分屏"

        return " ⇄ ".join(names) if len(names) > 1 else names[0]

    @property
    def hosts(self) -> list[Host]:
        """本标签里出现的所有主机（去重，保持面板顺序）。"""

        result: list[Host] = []

        for pane in self.panes:
            if pane.host is not None and all(item.id != pane.host.id for item in result):
                result.append(pane.host)

        return result

    def pane_for_host(self, host_id: str):
        for pane in self.panes:
            if pane.host is not None and pane.host.id == host_id:
                return pane

        return None

    @property
    def panes(self) -> list[SessionTab]:
        return [
            self.splitter.widget(index)
            for index in range(self.splitter.count())
            if isinstance(self.splitter.widget(index), SessionTab)
        ]

    def active_pane(self) -> SessionTab | None:
        panes = self.panes

        if not panes:
            return None

        focused = self.focusWidget()

        while focused is not None:
            if isinstance(focused, SessionTab):
                return focused

            focused = focused.parentWidget()

        return panes[0]

    # ------------------------------------------------------------------

    def add_pane(
        self,
        host: Host | None = None,
        credential: str | None = None,
        jump: SSHClient | None = None,
        activate: bool = True,
    ) -> SessionTab | None:
        """新增一个面板；``host`` 不传就沿用本标签的主机。

        传入别的主机即可实现"左边一台、右边另一台"的分屏。
        """

        if len(self.panes) >= self.MAX_PANES:
            return None

        pane_host = host if host is not None else self.host
        pane = SessionTab(
            pane_host,
            credential=credential if credential is not None else self.credential,
            jump=jump if jump is not None else self.jump,
        )
        pane.title_changed.connect(self._on_pane_title)
        pane.failed.connect(self.failed)
        pane.closed.connect(lambda p=pane: self._on_pane_closed(p))
        pane.input_generated.connect(lambda data, p=pane: self.pane_input.emit(p, data))

        # 先隐藏再插入，避免窗口已显示时插入控件触发 Qt 布局递归
        pane.setVisible(False)
        self.splitter.addWidget(pane)
        pane.setVisible(True)

        if activate:
            pane.view.setFocus()

        self.panes_changed.emit()

        return pane

    def split(self, orientation: Qt.Orientation) -> SessionTab | None:
        """按指定方向分屏（左右/上下都**最多两个面板**）。

        - 还没到上限：新增一个面板，返回它
        - 已经是两个面板：只**旋转现有布局**（左右 ↔ 上下），不再加面板，返回 None
        """

        if len(self.panes) < self.MAX_PANES:
            if self.splitter.orientation() != orientation:
                self.splitter.setOrientation(orientation)

            return self.add_pane(activate=True)
        # 已到上限：允许切换排列方向，但绝不新增面板
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)

        return None

    @property
    def orientation(self) -> Qt.Orientation:
        """当前分屏方向（左右=Horizontal，上下=Vertical）。"""

        return self.splitter.orientation()

    def focus_next(self) -> None:
        panes = self.panes

        if len(panes) < 2:
            return

        current = self.active_pane()
        index = panes.index(current) if current in panes else 0
        panes[(index + 1) % len(panes)].view.setFocus()

    # ------------------------------------------------------------------

    def _on_pane_title(self, title: str) -> None:
        self.title_changed.emit(title)

    def _on_pane_closed(self, pane: SessionTab) -> None:
        """单个面板会话结束：保留画面，但把它标记出来。"""

        if self._closing:
            return

        pane.status.setText("○ 会话已结束")
        pane.status.setStyleSheet("color: #8b949e; padding: 2px 8px;")

    def close_pane(self, pane: SessionTab) -> None:
        pane.close()
        pane.setParent(None)

        if len(self.panes) == 1:
            self.closed.emit()

    def close(self) -> None:
        self._closing = True

        for pane in self.panes:
            pane.close()

    def reconnect_all(self) -> None:
        for pane in self.panes:
            pane.reconnect(credential=self.credential, jump=self.jump)

    def reconcile(self) -> None:
        """重连或恢复后，把每个面板的终端尺寸再同步一次。"""

        for pane in self.panes:
            if pane.session is not None:
                pane.session.set_terminal_size(pane.view.emulator.columns, pane.view.emulator.lines)
