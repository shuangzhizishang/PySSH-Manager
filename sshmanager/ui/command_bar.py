"""命令输入栏：带完整编辑能力的命令框 + 发送/收藏/输出导出。

放在每个终端标签下方，用途：

- **Windows→Linux**：在框里写命令（支持多行脚本），Enter 发给远端；
  框本身是标准文本编辑器，复制/剪切/粘贴/撤销都用系统习惯的 Ctrl+C/X/V/Z。
- **Linux→Windows**：一键把终端输出复制到 Windows 剪贴板，或另存为文件。
- **收藏**：把当前框里的命令存进命令库（可分组、可在命令库里复用）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class CommandInput(QPlainTextEdit):
    """命令输入框：Enter 发送，Shift+Enter 换行。"""

    send_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.setPlaceholderText("在这里输入命令…Enter 发送到 Linux，Shift+Enter 换行（Ctrl+C/X/V/Z 正常编辑）")
        self.setTabChangesFocus(True)
        self.setFixedHeight(58)
        self.setStyleSheet(
            "QPlainTextEdit { background-color: #0d1117; color: #e6edf3;"
            " border: 1px solid #30363d; border-radius: 4px; padding: 4px; }"
        )

    def keyPressEvent(self, event):  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.send_requested.emit()
            return

        super().keyPressEvent(event)


class CommandBar(QWidget):
    """命令输入栏（输入框 + 操作按钮）。"""

    #: 请求把文本作为命令发送到当前会话
    command_requested = Signal(str)

    #: 请求把当前输入收藏到命令库
    favorite_requested = Signal(str)

    #: 请求复制终端全部输出（Linux→Windows）
    copy_output_requested = Signal()

    #: 请求把终端输出另存为文件
    save_output_requested = Signal()

    #: 请求打开命令库
    library_requested = Signal()

    #: 请求关闭输入栏
    close_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.setStyleSheet("background-color: #161b22;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)

        title = QLabel("命令")
        title.setStyleSheet("color: #8b949e;")
        header.addWidget(title)
        header.addStretch()

        self.hint = QLabel("Enter 发送 · Shift+Enter 换行")
        self.hint.setStyleSheet("color: #6e7681;")
        header.addWidget(self.hint)

        layout.addLayout(header)

        self.input = CommandInput()
        self.input.send_requested.connect(self._emit_command)
        layout.addWidget(self.input)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.send_button = QPushButton("发送 → Linux")
        self.send_button.setToolTip("把输入框内容作为命令发送到远端（也会记进命令历史）")
        self.send_button.clicked.connect(self._emit_command)

        self.favorite_button = QPushButton("收藏")
        self.favorite_button.setToolTip("把输入框里的命令存进命令库")

        self.library_button = QPushButton("命令库")
        self.library_button.setToolTip("打开命令收藏与历史（Ctrl+Shift+L）")

        self.copy_output_button = QPushButton("复制输出 ← Linux")
        self.copy_output_button.setToolTip("把终端全部输出复制到 Windows 剪贴板")

        self.save_output_button = QPushButton("保存输出…")
        self.save_output_button.setToolTip("把终端输出保存为文本文件")

        self.clear_button = QPushButton("清空")

        self.close_button = QPushButton("收起")
        self.close_button.setToolTip("隐藏命令输入栏（Ctrl+` 再打开）")

        for widget in (
            self.send_button,
            self.favorite_button,
            self.library_button,
            self.copy_output_button,
            self.save_output_button,
            self.clear_button,
        ):
            row.addWidget(widget)

        row.addStretch()
        row.addWidget(self.close_button)

        layout.addLayout(row)

        self.favorite_button.clicked.connect(lambda: self.favorite_requested.emit(self.input.toPlainText()))
        self.library_button.clicked.connect(self.library_requested.emit)
        self.copy_output_button.clicked.connect(self.copy_output_requested.emit)
        self.save_output_button.clicked.connect(self.save_output_requested.emit)
        self.clear_button.clicked.connect(self.input.clear)
        self.close_button.clicked.connect(self.close_requested.emit)

        # 面板级快捷键：和终端里的 Ctrl+Shift+C/V 区分开
        for sequence, handler in (
            ("Ctrl+Return", self._emit_command),
            ("Ctrl+Shift+S", lambda: self.favorite_requested.emit(self.input.toPlainText())),
            ("Ctrl+Shift+L", self.library_requested.emit),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)

    # ------------------------------------------------------------------

    def text(self) -> str:
        return self.input.toPlainText()

    def set_text(self, text: str) -> None:
        self.input.setPlainText(text)
        self.input.moveCursor(self.input.textCursor().MoveOperation.End)

    def focus_input(self) -> None:
        self.input.setFocus()

    def _emit_command(self) -> None:
        text = self.input.toPlainText().strip()

        if not text:
            return

        self.command_requested.emit(text)

    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.hint.setText(text)
