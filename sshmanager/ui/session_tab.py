"""一个会话标签页：终端视图 + 状态栏 + SSH 线程。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QLabel, QHBoxLayout, QVBoxLayout, QWidget

from ..hosts import Host
from ..session import SSHSession
from ..sshclient import SSHClient
from ..terminal.view import TerminalView


class SessionTab(QWidget):
    """把终端控件和一条 SSH 连接绑在一起。"""

    #: 标题变化（由远端 OSC 序列设置）
    title_changed = Signal(str)

    #: 线程结束
    closed = Signal()

    #: 需要向用户展示的错误（附指纹确认机会）
    error_raised = Signal(str)

    def __init__(
        self,
        host: Host,
        credential: str | None = None,
        jump: SSHClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential

        self._closing = False

        self.view = TerminalView()

        self.status = QLabel("○ 正在连接...")
        self.status.setStyleSheet("color: #d29922; padding: 2px 8px;")

        self.target = QLabel(host.target)
        self.target.setStyleSheet("color: #8b949e; padding: 2px 8px;")

        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.addWidget(self.status)
        bar_layout.addStretch()
        bar_layout.addWidget(self.target)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(bar)
        layout.addWidget(self.view, 1)

        self.view.apply_font(host.font_family, host.font_size)
        self.view.title_changed.connect(self._on_title)
        self.view.terminal_resized.connect(self._on_resized)
        self.view.data_to_send.connect(self._on_input)

        self.session = SSHSession(host, credential=credential, jump=jump)
        self.session.data_received.connect(self.view.feed)
        self.session.connected.connect(self._on_connected)
        self.session.failed.connect(self._on_failed)
        self.session.disconnected.connect(self._on_disconnected)
        self.session.finished.connect(self._on_finished)

        self.session.start()

        QTimer.singleShot(0, self.view.setFocus)

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return self.host.display_name

    def reconnect(self, credential: str | None = None, jump: SSHClient | None = None) -> None:
        if credential is not None:
            self.credential = credential

        self.session.request_stop()
        self.session.wait(2000)

        self.session = SSHSession(self.host, credential=self.credential, jump=jump)
        self.session.data_received.connect(self.view.feed)
        self.session.connected.connect(self._on_connected)
        self.session.failed.connect(self._on_failed)
        self.session.disconnected.connect(self._on_disconnected)
        self.session.finished.connect(self._on_finished)

        self._set_status("○ 正在连接...", "#d29922")
        self.session.start()

    def close(self) -> None:
        self._closing = True

        self.session.request_stop()
        self.session.client.close()

        if not self.session.wait(2000):
            self.session.terminate()
            self.session.wait(500)

    # ------------------------------------------------------------------

    def _on_input(self, data: bytes) -> None:
        self.session.send(data)

    def _on_resized(self, columns: int, rows: int) -> None:
        self.session.set_terminal_size(columns, rows)

    def _on_title(self, title: str) -> None:
        if title:
            self.title_changed.emit(title)

    def _on_connected(self, ) -> None:
        self._set_status("● 已连接", "#3fb950")

        columns, rows = self.view.emulator.columns, self.view.emulator.lines
        self.session.set_terminal_size(columns, rows)
        self.view.setFocus()

    def _on_failed(self, message: str) -> None:
        self._set_status("● 连接失败", "#f85149")
        self.view.feed(f"\r\n\x1b[31m[连接失败] {message}\x1b[0m\r\n".encode("utf-8"))
        self.error_raised.emit(message)

    def _on_disconnected(self) -> None:
        if self._closing:
            return

        self._set_status("○ 已断开", "#8b949e")
        self.view.feed("\r\n\x1b[33m[连接已关闭]\x1b[0m\r\n".encode("utf-8"))

    def _on_finished(self) -> None:
        self.closed.emit()

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; padding: 2px 8px;")
