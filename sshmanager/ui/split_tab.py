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
    """把一个主机拆成多个并排/堆叠的终端面板。"""

    title_changed = Signal(str)
    closed = Signal()
    failed = Signal(str)

    MAX_PANES = 4

    def __init__(
        self,
        host: Host,
        credential: str | None = None,
        jump: SSHClient | None = None,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent=None,
    ) -> None:
        super().__init__(parent)

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

        self.add_pane(activate=False)

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return self.host.display_name

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

    def add_pane(self, activate: bool = True) -> SessionTab | None:
        if len(self.panes) >= self.MAX_PANES:
            return None

        pane = SessionTab(self.host, credential=self.credential, jump=self.jump)
        pane.title_changed.connect(self._on_pane_title)
        pane.failed.connect(self.failed)
        pane.closed.connect(lambda p=pane: self._on_pane_closed(p))

        # 先隐藏再插入，避免窗口已显示时插入控件触发 Qt 布局递归
        pane.setVisible(False)
        self.splitter.addWidget(pane)
        pane.setVisible(True)

        if activate:
            pane.view.setFocus()

        return pane

    def split(self, orientation: Qt.Orientation) -> SessionTab | None:
        """按指定方向再开一个面板（与现有分屏方向一致）。"""

        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)

        return self.add_pane(activate=True)

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
