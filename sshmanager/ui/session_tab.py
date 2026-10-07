"""会话标签页：SSH 会话或本地终端，每个标签对应一个终端画面。

- :class:`TerminalTab`：纯终端画面（分屏容器复用它，不含状态栏）
- :class:`SessionTab`：带状态栏的完整标签页
- :class:`LocalTerminalTab`：本地终端（ConPTY）
"""

from __future__ import annotations

import uuid

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..commands import get_store
from ..hosts import Host
from ..localsession import LocalShellSession
from ..session import SSHSession
from ..sshclient import SSHClient
from ..terminal.view import TerminalView
from .command_bar import CommandBar
from .command_library import CommandLibraryDialog

LOCAL_ID = "local"

#: 无法在限定时间内 join 的会话线程。保留引用可避免 QThread 对象在仍运行时被
#: 垃圾回收（Qt 会打印 "QThread: Destroyed while thread is still running" 并可能崩溃）。
_ORPHANED_SESSIONS: list = []


def make_local_host(shell_command: str | None = None) -> Host:
    """造一个代表本地终端的主机对象（仅用于界面归类，不持久化）。"""

    return Host(
        label="本地终端",
        hostname=shell_command or "local",
        port=0,
        username="",
        auth_type="local",
        verify_host_key=False,
    )


def drain_sessions(timeout: float = 5.0) -> int:
    """等待仍在运行的会话线程结束，返回未结束的数量。

    ``_ORPHANED_SESSIONS`` 里的线程是在"连接中途关窗"等场景下没能及时 join 的，
    退出前再等一次；只要底层连接已关闭，它们通常很快结束。
    """

    remaining = 0

    for session in list(_ORPHANED_SESSIONS):
        try:
            if session.wait(int(timeout * 1000)):
                _ORPHANED_SESSIONS.remove(session)
            else:
                remaining += 1
        except Exception:
            remaining += 1

    return remaining


class TerminalTab(QWidget):
    """一个终端画面 + 一条会话（SSH 或本地）。

    默认不自动启动，由子类在界面搭好后调用 :meth:`start`。
    """

    title_changed = Signal(str)
    closed = Signal()
    failed = Signal(str)

    def __init__(
        self,
        host: Host | None = None,
        credential: str | None = None,
        jump: SSHClient | None = None,
        shell_command: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential
        self.jump = jump
        self.shell_command = shell_command

        self._closing = False
        self.status_hint = "○ 正在连接..."
        self.recorder = None
        #: 远端通过 OSC 序列设置的标题（优先用于标签名）
        self.remote_title = ""

        self.view = TerminalView()
        self.view.data_to_send.connect(self._on_input)
        self.view.terminal_resized.connect(self._on_resized)
        self.view.title_changed.connect(self._on_title)
        self.view.copied.connect(self._on_copied)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(self.view, 1)

        # 命令输入栏：写命令（Windows→Linux）、把输出拷回 Windows、收藏命令
        self.command_bar = CommandBar()
        self.command_bar.command_requested.connect(self.send_command)
        self.command_bar.favorite_requested.connect(self.favorite_command)
        self.command_bar.copy_output_requested.connect(self.copy_output)
        self.command_bar.save_output_requested.connect(self.save_output)
        self.command_bar.library_requested.connect(self.open_command_library)
        self.command_bar.close_requested.connect(lambda: self.set_command_bar_visible(False))
        self._layout.addWidget(self.command_bar)

        if host is not None:
            self.view.apply_font(host.font_family, host.font_size)

        self.session = None

    # ------------------------------------------------------------------
    # 命令输入栏
    # ------------------------------------------------------------------

    def set_command_bar_visible(self, visible: bool) -> None:
        self.command_bar.setVisible(bool(visible))

    def toggle_command_bar(self) -> None:
        self.set_command_bar_visible(not self.command_bar.isVisible())

    @property
    def command_bar_visible(self) -> bool:
        return self.command_bar.isVisible()

    def send_command(self, text: str) -> None:
        """把文本作为命令发送到远端（多行会逐行执行），并记进命令历史。"""

        payload = (text or "").strip("\n")

        if not payload.strip():
            return

        store = get_store()
        store.record_history(payload, self._host_id())

        # 终端要求 \r 作为换行；多行脚本逐行下发
        normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
        data = normalized.replace("\n", "\r") + "\r"
        self._on_input(data.encode("utf-8"))
        self.command_bar.set_status(f"已发送 {len(normalized.splitlines())} 行到 Linux")

    def favorite_command(self, text: str) -> None:
        text = (text or "").strip()

        if not text:
            self.command_bar.set_status("输入框为空，先写点命令再收藏")
            return

        snippet = get_store().add(text, host_id=self._host_id())

        if snippet is None:
            self.command_bar.set_status("这条命令已经在收藏里了")
            return

        self.command_bar.set_status("已收藏到命令库（Ctrl+Shift+L 打开）")

    def open_command_library(self) -> None:
        dialog = CommandLibraryDialog(
            get_store(),
            host_id=self._host_id(),
            host_label=self.display_name,
            parent=self,
        )
        dialog.send_requested.connect(self.send_command)
        dialog.exec()

    def copy_output(self) -> None:
        text = self.view.copy_all_output()

        if text:
            self.command_bar.set_status(f"已复制 {len(text.splitlines())} 行输出到 Windows 剪贴板")
        else:
            self.command_bar.set_status("终端还没有输出")

    def save_output(self) -> None:
        default = f"{self.display_name.replace(':', '_').replace('/', '_')}-输出.txt".replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(self, "保存终端输出", default, "文本文件 (*.txt);;所有文件 (*)")

        if not path:
            return

        size = self.view.save_output(path)
        self.command_bar.set_status(f"已保存到 {path}（{size} 字节）")

    def _host_id(self) -> str:
        return self.host.id if self.host is not None else LOCAL_ID

    def _on_copied(self, text: str) -> None:
        if self.command_bar.isVisible():
            self.command_bar.set_status(f"已复制 {len(text)} 字符到 Windows 剪贴板")

    # ------------------------------------------------------------------

    @property
    def is_local(self) -> bool:
        return self.shell_command is not None or (self.host is not None and self.host.auth_type == "local")

    @property
    def display_name(self) -> str:
        if self.remote_title:
            return self.remote_title

        if self.host is not None:
            return self.host.display_name

        return "本地终端"

    def start(self) -> None:
        self.session = self._create_session()

        # 先包一层：录制输出要与喂给终端的是同一份数据
        self.session.data_received.connect(self._on_output)

        self.session.connected.connect(self._on_connected)
        self.session.failed.connect(self._on_failed)
        self.session.disconnected.connect(self._on_disconnected)
        self.session.finished.connect(self._on_finished)
        self.session.start()

        QTimer.singleShot(0, self.view.setFocus)

    def _on_output(self, data: bytes) -> None:
        """服务器输出：喂给终端 +（可选）写入录制。"""

        self.view.feed(data)

        if self.recorder is not None and self.recorder.active:
            try:
                self.recorder.write_output(data)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 会话录制
    # ------------------------------------------------------------------

    def start_recording(self, path, width: int | None = None, height: int | None = None):
        """开始录制，返回录制文件路径。"""

        from ..recording import CastRecorder

        if self.recorder is not None and self.recorder.active:
            return self.recorder.path

        self.recorder = CastRecorder(
            path,
            width=width or self.view.emulator.columns,
            height=height or self.view.emulator.lines,
            title=self.display_name,
        )
        self.recorder.start()

        return self.recorder.path

    def stop_recording(self):
        if self.recorder is None or not self.recorder.active:
            return None

        return self.recorder.stop()

    @property
    def recording(self) -> bool:
        return self.recorder is not None and self.recorder.active

    def _record_resize(self, columns: int, rows: int) -> None:
        if self.recorder is not None and self.recorder.active:
            try:
                self.recorder.write_resize(columns, rows)
            except Exception:
                pass

    def _create_session(self):
        if self.is_local:
            return LocalShellSession(command=self.shell_command)

        return SSHSession(self.host, credential=self.credential, jump=self.jump)

    def _stop_session(self) -> None:
        if self.session is None:
            return

        if hasattr(self.session, "mark_closing"):
            self.session.mark_closing()

        # 先打断阻塞中的连接/读取，避免线程在进程退出阶段还挂在 recv 上
        if hasattr(self.session, "cancel"):
            self.session.cancel()
        else:
            self.session.request_stop()

        if self.session.wait(5000):
            return

        # 还在跑（常见于"连接/认证中途关窗"）：再断一次并等，最后把对象留在
        # 模块级列表里，避免 QThread 在运行中被销毁
        self.session.request_stop()
        client = getattr(self.session, "client", None)

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        if not self.session.wait(3000):
            _ORPHANED_SESSIONS.append(self.session)

    # ------------------------------------------------------------------

    def reconnect(self, credential: str | None = None, jump: SSHClient | None = None) -> None:
        if self.is_local:
            return

        if credential is not None:
            self.credential = credential

        if jump is not None:
            self.jump = jump

        self._stop_session()
        self._on_status_changed("○ 正在连接...", "#d29922")
        self.start()

    def close(self) -> None:
        self._closing = True
        self.stop_recording()
        self._stop_session()

    # ------------------------------------------------------------------

    def _on_input(self, data: bytes) -> None:
        if self.session is not None:
            self.session.send(data)

        if self.recorder is not None and self.recorder.active:
            try:
                self.recorder.write_input(data)
            except Exception:
                pass

    def _on_resized(self, columns: int, rows: int) -> None:
        if self.session is not None:
            self.session.set_terminal_size(columns, rows)

        self._record_resize(columns, rows)

    def _on_title(self, title: str) -> None:
        if title:
            self.title_changed.emit(title)

    def _on_connected(self) -> None:
        self._on_status_changed("● 已连接", "#3fb950")

        if self.session is not None:
            self.session.set_terminal_size(self.view.emulator.columns, self.view.emulator.lines)

        if self.isVisible():
            self.view.setFocus()

    def _on_failed(self, message: str) -> None:
        self._on_status_changed("● 连接失败", "#f85149")
        self.view.feed(f"\r\n\x1b[31m[连接失败] {message}\x1b[0m\r\n".encode("utf-8"))
        self.failed.emit(message)

    def _on_disconnected(self) -> None:
        if self._closing:
            return

        self._on_status_changed("○ 已断开", "#8b949e")
        self.view.feed("\r\n\x1b[33m[连接已关闭]\x1b[0m\r\n".encode("utf-8"))

    def _on_finished(self) -> None:
        self.closed.emit()

    def _on_status_changed(self, text: str, color: str) -> None:
        """子类覆盖以更新状态栏；基类只记录文本。"""

        self.status_hint = text


class SessionTab(TerminalTab):
    """带状态栏的完整标签页。"""

    error_raised = Signal(str)

    def __init__(
        self,
        host: Host | None = None,
        credential: str | None = None,
        jump: SSHClient | None = None,
        shell_command: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(
            host=host,
            credential=credential,
            jump=jump,
            shell_command=shell_command,
            parent=parent,
        )

        self._build_status_bar()
        self.failed.connect(self.error_raised)
        self.start()

    def _build_status_bar(self) -> None:
        self.status = QLabel("○ 正在连接...")
        self.status.setStyleSheet("color: #d29922; padding: 2px 8px;")

        self.target = QLabel(self._target_text())
        self.target.setStyleSheet("color: #8b949e; padding: 2px 8px;")

        bar = QWidget()
        bar.setStyleSheet("background-color: #161b22;")

        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.status)
        row.addStretch()
        row.addWidget(self.target)

        self._layout.insertWidget(0, bar)

    def _target_text(self) -> str:
        if self.host is None:
            return "本地"

        return self.host.hostname if self.is_local else self.host.target

    def _on_status_changed(self, text: str, color: str) -> None:
        self.status_hint = text
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; padding: 2px 8px;")


class LocalTerminalTab(SessionTab):
    """本地终端标签。"""

    def __init__(self, shell_command: str | None = None, parent=None) -> None:
        super().__init__(
            host=make_local_host(shell_command),
            shell_command=shell_command,
            parent=parent,
        )

        self.host.id = f"{LOCAL_ID}-{uuid.uuid4().hex[:8]}"
