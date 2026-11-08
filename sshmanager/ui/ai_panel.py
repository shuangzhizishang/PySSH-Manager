"""DeepSeek AI 助手面板。

能力：

- 与 AI 对话（流式显示），自动附带当前主机环境（发行版/包管理器）与终端最近输出
- 把回复里的 ```bash 代码块渲染成"建议命令"卡片：执行 / 插入终端 / 复制
- 执行后自动把真实输出回传给 AI 继续分析（可关）
- 危险命令二次确认
- 一键诊断（只读命令走独立通道，不打扰终端）
- AI 也能通过 ```pyssh-action 操作软件（打开 SFTP、分屏、同步输入…）
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..ai import AiConfig, DeepSeekClient, SuggestedCommand, env_api_key, extract_commands
from ..ai.actions import SuggestedAction, extract_actions, strip_action_blocks
from ..ai.commands import insert_plan
from ..ai.config import CREDENTIAL_KEY
from ..ai.context import DIAGNOSE_COMMAND, terminal_tail
from ..ai.prompts import build_context_message, build_system_prompt
from ..ai.worker import AiCaptureWorker, AiChatWorker, AiEnvWorker

RISK_COLORS = {"safe": "#3fb950", "careful": "#d29922", "danger": "#f85149"}


class ChatInput(QPlainTextEdit):
    """回车发送、Shift+回车换行。"""

    submitted = Signal()

    def keyPressEvent(self, event):  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submitted.emit()
                event.accept()
                return

        super().keyPressEvent(event)


class CommandCard(QFrame):
    """一条建议命令。"""

    def __init__(self, command: SuggestedCommand, panel: "AiPanel", parent=None) -> None:
        super().__init__(parent)

        self.command = command
        self.panel = panel

        self.setStyleSheet(
            "QFrame { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        color = RISK_COLORS.get(command.risk, "#8b949e")
        badge = QLabel(command.risk_label)
        badge.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 4px;"
            " padding: 0px 6px; font-size: 11px;"
        )
        badge.setToolTip(command.reason or command.risk_label)

        title = QLabel(command.title or "命令")
        title.setStyleSheet("color: #e6edf3; font-weight: bold;")
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        head.addWidget(badge)
        head.addWidget(title, 1)
        layout.addLayout(head)

        self.code_edit = QPlainTextEdit(command.code)
        self.code_edit.setReadOnly(True)
        self.code_edit.setFont(panel.mono_font())
        lines = max(1, min(command.code.count("\n") + 1, 12))
        self.code_edit.setFixedHeight(18 + 18 * lines)
        self.code_edit.setStyleSheet(
            "QPlainTextEdit { background-color: #0d1117; color: #e6edf3; border: 1px solid #21262d;"
            " border-radius: 6px; padding: 4px; }"
        )
        self.code_edit.setToolTip(command.reason or "")
        layout.addWidget(self.code_edit)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("执行")
        self.run_button.setToolTip("在当前终端执行这条命令（执行后可自动把输出交给 AI）")
        self.run_button.clicked.connect(lambda: panel.execute_command(command, self))

        insert_button = QPushButton("插入终端")
        insert_button.setToolTip("只把命令放进终端，不回车（你可以先改一改）")
        insert_button.clicked.connect(lambda: panel.insert_command(command))

        copy_button = QPushButton("复制")
        copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(command.code))

        buttons.addWidget(self.run_button)
        buttons.addWidget(insert_button)
        buttons.addWidget(copy_button)
        buttons.addStretch()
        layout.addLayout(buttons)

    def mark_executed(self) -> None:
        self.run_button.setText("已执行 ✓")
        self.run_button.setEnabled(False)


class ActionCard(QFrame):
    """一条建议的软件操作。"""

    def __init__(self, suggestion: SuggestedAction, panel: "AiPanel", parent=None) -> None:
        super().__init__(parent)

        self.setStyleSheet(
            "QFrame { background-color: #11161d; border: 1px dashed #30363d; border-radius: 8px; }"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)

        label = QLabel(f"⚙ {suggestion.action.label}")
        label.setStyleSheet("color: #58a6ff;")
        tooltip = suggestion.action.description

        if suggestion.reason:
            tooltip += f"\n原因：{suggestion.reason}"

        label.setToolTip(tooltip)

        button = QPushButton("执行操作")
        button.clicked.connect(lambda: panel.run_app_action(suggestion.action.name, self))
        button.setToolTip(suggestion.action.description)

        layout.addWidget(label, 1)
        layout.addWidget(button)


class AiPanel(QWidget):
    """右侧 AI 助手面板。"""

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)

        self.context = context
        self.config = AiConfig.load()

        self._pane = None
        self._environment: dict = {}
        self._env_worker = None
        self._capture_worker = None
        self._worker: AiChatWorker | None = None
        self._stream_text = ""
        self._transcript: list[dict] = []
        self._watch: dict | None = None
        self._action_handler = None

        self.setMinimumWidth(320)
        self._build_ui()
        self._refresh_header()
        self.attach_pane(None)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def mono_font(self):
        from PySide6.QtGui import QFont

        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)

        return font

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.target_label = QLabel("目标：未连接")
        self.target_label.setStyleSheet("color: #8b949e; font-size: 11px;")
        self.model_label = QLabel("")
        self.model_label.setStyleSheet("color: #58a6ff; font-size: 11px;")

        settings_button = QPushButton("⚙")
        settings_button.setFixedWidth(32)
        settings_button.setToolTip("AI 设置（API Key / 模型 / 接口地址）")
        settings_button.clicked.connect(self.open_settings)

        clear_button = QPushButton("清空")
        clear_button.setFixedWidth(48)
        clear_button.setToolTip("清空对话")
        clear_button.clicked.connect(self.clear_chat)

        header.addWidget(self.target_label, 1)
        header.addWidget(self.model_label)
        header.addWidget(settings_button)
        header.addWidget(clear_button)
        layout.addLayout(header)

        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(False)
        self.chat_view.setStyleSheet(
            "QTextBrowser { background-color: #0d1117; border: 1px solid #21262d;"
            " border-radius: 6px; padding: 6px; }"
        )
        layout.addWidget(self.chat_view, 1)

        self.suggestions_area = QScrollArea()
        self.suggestions_area.setWidgetResizable(True)
        self.suggestions_area.setMaximumHeight(240)
        self.suggestions_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.suggestions_inner = QWidget()
        self.suggestions_layout = QVBoxLayout(self.suggestions_inner)
        self.suggestions_layout.setContentsMargins(0, 0, 0, 0)
        self.suggestions_layout.setSpacing(6)
        self.suggestions_layout.addStretch()
        self.suggestions_area.setWidget(self.suggestions_inner)
        self.suggestions_area.setVisible(False)
        layout.addWidget(self.suggestions_area)

        quick = QHBoxLayout()

        for label, handler, tip in (
            ("诊断机器", self.diagnose_current, "采集系统/磁盘/内存/服务状态并让 AI 分析"),
            ("装环境", self.ask_install_env, "按发行版给出常用开发/运维环境安装命令"),
            ("磁盘排查", self.ask_disk_check, "磁盘满 / 内存不足的排查"),
            ("解释输出", self.explain_output, "让 AI 解释终端最近的输出"),
        ):
            button = QPushButton(label)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            quick.addWidget(button)

        layout.addLayout(quick)

        self.input_edit = ChatInput()
        self.input_edit.setPlaceholderText(
            "描述你想做的事，例如：帮我在这台机器上装 MySQL 8（回车发送，Shift+回车换行）"
        )
        self.input_edit.setFixedHeight(64)
        self.input_edit.submitted.connect(self.send)
        layout.addWidget(self.input_edit)

        footer = QHBoxLayout()
        self.include_env = QCheckBox("环境")
        self.include_env.setChecked(True)
        self.include_env.setToolTip("把发行版/包管理器等信息一起发给 AI")

        self.include_tail = QCheckBox("终端输出")
        self.include_tail.setChecked(self.config.include_terminal_tail > 0)
        self.include_tail.setToolTip("把终端最近若干行输出一起发给 AI")

        self.auto_analyze = QCheckBox("自动分析")
        self.auto_analyze.setChecked(bool(self.config.auto_analyze_result))
        self.auto_analyze.setToolTip("执行命令后自动把输出回传给 AI 继续分析")

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8b949e; font-size: 11px;")

        self.send_button = QPushButton("发送")
        self.send_button.clicked.connect(self.send)

        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_reply)

        footer.addWidget(self.include_env)
        footer.addWidget(self.include_tail)
        footer.addWidget(self.auto_analyze)
        footer.addStretch()
        footer.addWidget(self.send_button)
        footer.addWidget(self.stop_button)
        layout.addLayout(footer)

        layout.addWidget(self.status_label)

        QShortcut(QKeySequence("Ctrl+Return"), self.input_edit, activated=self.send)

    # ------------------------------------------------------------------
    # 对外接口（主窗口调用）
    # ------------------------------------------------------------------

    def set_action_handler(self, handler) -> None:
        """主窗口注册"执行软件操作"的回调：``handler(name) -> bool``。"""

        self._action_handler = handler

    def attach_pane(self, pane) -> None:
        """切换当前目标会话（主窗口在标签切换/新建会话时调用）。"""

        try:
            same = pane is self._pane
        except RuntimeError:
            same = False

        if same:
            return

        self._pane = pane
        self._environment = {}

        if not self._pane_alive(pane):
            self.target_label.setText("目标：未连接（先在左侧打开一个会话）")
            self._set_status("先连接一台主机，再让 AI 帮你操作", "#d29922")
            return

        host = getattr(pane, "host", None)

        if host is None or getattr(pane, "is_local", False):
            self.target_label.setText("目标：本地 Windows 终端")
            self._set_status("本地终端不采集 Linux 环境", "#8b949e")
            return

        self.target_label.setText(f"目标：{host.display_name}")
        self.target_label.setToolTip(f"{host.username}@{host.hostname}:{host.port}")
        self._start_env_capture(pane)

    def open_settings(self) -> None:
        from .ai_settings import AiSettingsDialog

        dialog = AiSettingsDialog(self.context.credentials, self)

        if dialog.exec() == AiSettingsDialog.DialogCode.Accepted:
            self.config = AiConfig.load()
            self._refresh_header()
            self.include_tail.setChecked(self.config.include_terminal_tail > 0)
            self.auto_analyze.setChecked(bool(self.config.auto_analyze_result))
            self._set_status("设置已保存", "#3fb950")

    def clear_chat(self) -> None:
        self._transcript.clear()
        self._stream_text = ""
        self._clear_cards()
        self.chat_view.clear()
        self._set_status("已清空对话", "#8b949e")

    def shutdown(self) -> None:
        """窗口退出时调用：停掉后台线程。"""

        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(1500)
            self._worker = None

        if self._env_worker is not None:
            self._env_worker.wait(1500)
            self._env_worker = None

        if self._capture_worker is not None:
            self._capture_worker.wait(1500)
            self._capture_worker = None

        if self._watch is not None:
            timer = self._watch.get("timer")

            if timer is not None:
                timer.stop()

            self._watch = None

    # ------------------------------------------------------------------
    # 会话与配置
    # ------------------------------------------------------------------

    @staticmethod
    def _pane_alive(pane) -> bool:
        if pane is None:
            return False

        try:
            _ = pane.view
            _ = pane.host
            return True
        except RuntimeError:
            return False

    def _api_key(self) -> str:
        stored = self.context.credentials.get(CREDENTIAL_KEY) or ""

        return stored.strip() or (env_api_key() or "")

    def has_api_key(self) -> bool:
        """是否已配置 API Key（凭据库或环境变量）。"""

        return bool(self._api_key())

    def _make_client(self) -> DeepSeekClient | None:
        key = self._api_key()

        if not key:
            return None

        return DeepSeekClient(key, self.config)

    def _refresh_header(self) -> None:
        self.model_label.setText(self.config.model)
        self.model_label.setToolTip(f"接口：{self.config.normalized_base_url()}")

        if not self._api_key():
            self._set_status("未配置 API Key：点右上角 ⚙ 填写（或设置 PYSSH_AI_API_KEY）", "#d29922")
        else:
            self._set_status("就绪", "#3fb950")

    def _start_env_capture(self, pane) -> None:
        session = getattr(pane, "session", None)
        client = getattr(session, "client", None) if session is not None else None

        if client is None:
            self._set_status("等待会话建立后采集环境 …", "#8b949e")
            return

        self._set_status("正在采集环境（发行版/包管理器）…", "#8b949e")

        worker = AiEnvWorker(client, self)
        worker.done.connect(self._on_env_ready)
        worker.finished.connect(worker.deleteLater)
        self._env_worker = worker
        worker.start()

    def _on_env_ready(self, environment: dict) -> None:
        self._environment = environment or {}
        self._env_worker = None

        if not self._environment:
            self._set_status("环境采集失败，仍可对话（AI 会先问发行版）", "#d29922")
            return

        name = self._environment.get("pretty_name") or self._environment.get("distro_id") or "Linux"
        managers = self._environment.get("package_managers") or ""
        self.target_label.setToolTip(
            f"{name}\n架构：{self._environment.get('uname', '')}\n包管理器：{managers}"
        )
        self._set_status(f"环境：{name[:26]} · {managers or '未知包管理器'}", "#3fb950")

    # ------------------------------------------------------------------
    # 发送 / 流式
    # ------------------------------------------------------------------

    def send(self, text: str | None = None) -> None:
        if self._worker is not None:
            self._set_status("AI 正在回复，稍等或点『停止』", "#d29922")
            return

        from_input = text is None

        if from_input:
            text = self.input_edit.toPlainText()

        text = (text or "").strip()

        if not text:
            return

        client = self._make_client()

        if client is None:
            QMessageBox.information(
                self,
                "还没有配置 API Key",
                "请先填写 DeepSeek API Key（在 platform.deepseek.com 创建）。\n"
                "也可以设置环境变量 PYSSH_AI_API_KEY 后重启。",
            )
            self.open_settings()
            return

        if from_input:
            self.input_edit.clear()

        self._transcript.append({"kind": "user", "text": text})
        self._render_chat()
        self._stream_text = ""
        self._clear_cards()
        self._set_busy(True)

        messages = self._build_messages()

        worker = AiChatWorker(client, messages, self)
        worker.delta.connect(self._on_delta)
        worker.reasoning.connect(self._on_reasoning)
        worker.finished_ok.connect(self._on_reply)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _build_messages(self) -> list[dict]:
        messages = [{"role": "system", "content": build_system_prompt(self.config, self._environment)}]

        # 除最后一条（本轮提问）以外的历史
        for item in self._transcript[:-1]:
            if item["kind"] in ("user", "assistant"):
                messages.append({"role": item["kind"], "content": item["text"]})

        context = self._context_block()
        last = self._transcript[-1]["text"] if self._transcript else ""
        messages.append({"role": "user", "content": context + last})

        return messages

    def _context_block(self) -> str:
        environment = self._environment if self.include_env.isChecked() else None
        tail = ""

        if self.include_tail.isChecked() and self._pane_alive(self._pane):
            tail = terminal_tail(self._pane.view, self.config.include_terminal_tail)

        if not environment and not tail:
            return ""

        return build_context_message(environment, tail)

    def _on_delta(self, text: str) -> None:
        self._stream_text += text
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat_view.setTextCursor(cursor)
        self.chat_view.insertPlainText(text)
        self.chat_view.ensureCursorVisible()
        self._set_status("AI 正在回复 …", "#58a6ff")

    def _on_reasoning(self, _text: str) -> None:
        self._set_status("AI 正在推理（deepseek-reasoner）…", "#d29922")

    def _on_reply(self, text: str) -> None:
        self._transcript.append({"kind": "assistant", "text": text})
        self._render_chat()
        self._build_cards(text)
        self._set_status(f"收到回复（{len(text)} 字）", "#3fb950")

    def _on_failed(self, message: str) -> None:
        self._transcript.append({"kind": "error", "text": message})
        self._transcript.append({"kind": "assistant", "text": f"（请求失败：{message}）"})
        self._render_chat()
        self._set_status("请求失败", "#f85149")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._set_busy(False)

    def stop_reply(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._set_status("已停止", "#d29922")

        self.stop_button.setEnabled(False)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def _render_chat(self) -> None:
        parts = []

        for item in self._transcript:
            kind = item["kind"]
            text = item["text"]

            if kind == "user":
                parts.append("**你：**\n\n" + text + "\n")
            elif kind == "assistant":
                parts.append("**AI：**\n\n" + strip_action_blocks(text) + "\n")
            elif kind == "note":
                parts.append("> " + text.replace("\n", "\n> ") + "\n")
            elif kind == "error":
                parts.append("> ⚠ " + text.replace("\n", "\n> ") + "\n")

        self.chat_view.setMarkdown("\n".join(parts))
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.chat_view.setTextCursor(cursor)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.input_edit.setReadOnly(busy)

        if not busy:
            self.input_edit.setFocus()

    def _set_status(self, text: str, color: str = "#8b949e") -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _note(self, text: str) -> None:
        self._transcript.append({"kind": "note", "text": text})
        self._render_chat()

    # ------------------------------------------------------------------
    # 建议命令 / 操作
    # ------------------------------------------------------------------

    def _clear_cards(self) -> None:
        while self.suggestions_layout.count() > 1:
            item = self.suggestions_layout.takeAt(0)
            widget = item.widget()

            if widget is not None:
                widget.deleteLater()

        self.suggestions_area.setVisible(False)

    def _build_cards(self, text: str) -> None:
        commands = extract_commands(text)
        actions = extract_actions(text)

        if not commands and not actions:
            return

        for command in commands:
            self.suggestions_layout.insertWidget(self.suggestions_layout.count() - 1, CommandCard(command, self))

        for suggestion in actions:
            self.suggestions_layout.insertWidget(self.suggestions_layout.count() - 1, ActionCard(suggestion, self))

        self.suggestions_area.setVisible(True)

    def execute_command(self, command: SuggestedCommand, card: CommandCard | None = None) -> None:
        pane = self._pane

        if not self._pane_alive(pane):
            QMessageBox.information(self, "没有活动会话", "请先在左侧连接一台主机，再执行命令。")
            return

        if command.risk == "danger":
            reply = QMessageBox.warning(
                self,
                "危险命令确认",
                f"这条命令被识别为危险操作：{command.reason}\n\n{command.code}\n\n确定要执行吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if reply != QMessageBox.StandardButton.Yes:
                return

        pane.send_command(command.code)

        if card is not None:
            card.mark_executed()

        self._note(f"已执行：{command.code.splitlines()[0][:100] if command.code else ''}")

        if self.auto_analyze.isChecked():
            self._watch_result(pane, command)

    def insert_command(self, command: SuggestedCommand) -> None:
        pane = self._pane

        if not self._pane_alive(pane):
            QMessageBox.information(self, "没有活动会话", "请先连接一台主机。")
            return

        code = command.code
        first_line, rest = insert_plan(code)

        if not first_line:
            self._set_status("没有可插入的内容", "#d29922")
            return

        try:
            pane.view.insert_text(first_line)
        except Exception:  # noqa: BLE001
            QGuiApplication.clipboard().setText(code)
            self._set_status("已复制到剪贴板（终端插入失败）", "#d29922")
            return

        if rest:
            QGuiApplication.clipboard().setText(code)
            self._set_status(f"已插入第 1 行，其余 {rest} 行已复制到剪贴板", "#d29922")
            self._note("多行脚本只插入第一行，完整内容已放到剪贴板")

    def run_app_action(self, name: str, card: ActionCard | None = None) -> None:
        if self._action_handler is None:
            self._set_status("当前版本不支持执行该操作", "#d29922")
            return

        ok = bool(self._action_handler(name))

        if card is not None and ok:
            card.setEnabled(False)

        self._note(f"已执行软件操作：{name}" if ok else f"软件操作未执行：{name}")

    # ------------------------------------------------------------------
    # 执行结果回传
    # ------------------------------------------------------------------

    def _watch_result(self, pane, command: SuggestedCommand) -> None:
        session = getattr(pane, "session", None)

        if session is None:
            return

        last = [time.monotonic()]
        started = time.monotonic()

        def on_data(_data: bytes) -> None:
            last[0] = time.monotonic()

        try:
            session.data_received.connect(on_data)
        except (RuntimeError, TypeError):
            return

        timer = QTimer(self)
        timer.setInterval(300)

        def check() -> None:
            now = time.monotonic()
            quiet = now - last[0]
            timed_out = now - started > 40

            if quiet < 1.0 and not timed_out:
                return

            timer.stop()
            self._watch = None

            try:
                session.data_received.disconnect(on_data)
            except (RuntimeError, TypeError):
                pass

            if not self._pane_alive(pane):
                return

            tail = terminal_tail(pane.view, max(20, self.config.include_terminal_tail))
            note = (
                f"我执行了这条命令：\n```bash\n{command.code}\n```\n"
                f"终端最近输出：\n```text\n{tail}\n```\n"
                "请判断是否成功，并指出下一步该做什么（如果有错误，直接给出修复命令）。"
            )
            self.send(note)

        timer.timeout.connect(check)
        timer.start()
        self._watch = {"timer": timer}

    # ------------------------------------------------------------------
    # 快捷动作
    # ------------------------------------------------------------------

    def _client_for_pane(self):
        pane = self._pane

        if not self._pane_alive(pane):
            return None

        session = getattr(pane, "session", None)

        return getattr(session, "client", None) if session is not None else None

    def diagnose_current(self) -> None:
        client = self._client_for_pane()

        if client is None:
            QMessageBox.information(self, "没有活动会话", "请先连接一台主机。")
            return

        self._set_status("正在采集系统状态（只读）…", "#8b949e")

        worker = AiCaptureWorker(client, DIAGNOSE_COMMAND, self)
        worker.done.connect(self._on_diagnose_done)
        worker.finished.connect(worker.deleteLater)
        self._capture_worker = worker
        worker.start()

    def _on_diagnose_done(self, text: str) -> None:
        self._capture_worker = None
        self._set_status("已采集系统状态", "#3fb950")
        self.send(
            "请诊断这台机器的健康状况（系统版本、磁盘、内存、负载、失败的服务）。\n"
            "先给一句总体结论，再按需要给出命令；如果一切正常也要明确说。\n\n"
            "以下是只读诊断命令的真实输出：\n```text\n" + (text or "（无输出）") + "\n```"
        )

    def ask_install_env(self) -> None:
        self.send(
            "我想在这台机器上装一套常用的开发/运维环境：git、vim、wget、curl、编译工具"
            "（gcc/make）、Python3、pip、tar、unzip、htop。\n"
            "请按当前发行版给出安装命令（一条命令一个代码块），并给出验证命令。"
        )

    def ask_disk_check(self) -> None:
        self.send(
            "帮我排查这台机器的磁盘和内存情况：可能磁盘满了或者内存不足。\n"
            "先给诊断命令，再根据输出告诉我怎么清理/扩容。"
        )

    def explain_output(self) -> None:
        if not self._pane_alive(self._pane):
            QMessageBox.information(self, "没有活动会话", "请先连接一台主机。")
            return

        tail = terminal_tail(self._pane.view, max(30, self.config.include_terminal_tail))

        if not tail.strip():
            self._set_status("终端还没有输出", "#d29922")
            return

        self.send("请解释下面这段终端输出：有没有报错？下一步该做什么？\n\n```text\n" + tail + "\n```")
