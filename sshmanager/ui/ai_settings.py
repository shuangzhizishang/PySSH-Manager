"""AI 助手设置：API Key、模型、接口地址与行为开关。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..ai import AiConfig, DEFAULT_BASE_URL, MODEL_CHOICES, env_api_key
from ..ai.config import CREDENTIAL_KEY
from ..ai.worker import AiTestWorker
from ..credentials import CredentialStore


class AiSettingsDialog(QDialog):
    """DeepSeek API 设置。

    API Key 只写进加密凭据库，不落明文；也可以不填，改用环境变量
    ``PYSSH_AI_API_KEY`` / ``DEEPSEEK_API_KEY``。
    """

    def __init__(self, credentials: CredentialStore, parent=None) -> None:
        super().__init__(parent)

        self.credentials = credentials
        self.config = AiConfig.load()
        self._test_worker: AiTestWorker | None = None
        self._saved_key = ""

        self.setWindowTitle("DeepSeek AI 设置")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        hint = QLabel(
            "填入 DeepSeek 的 API Key（在 platform.deepseek.com 创建）。\n"
            "Key 会存进 Windows 凭据加密（DPAPI），不会写进配置文件。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(8)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…（留空则使用环境变量）")

        stored = self.credentials.get(CREDENTIAL_KEY) or ""

        if stored:
            self.key_edit.setText(stored)
            self._saved_key = stored

        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(
            lambda checked: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )

        paste_button = QPushButton("粘贴")
        paste_button.clicked.connect(self._paste_key)

        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.show_key)
        key_row.addWidget(paste_button)
        form.addRow("API Key", key_row)

        self.base_edit = QLineEdit(self.config.normalized_base_url())
        self.base_edit.setPlaceholderText(DEFAULT_BASE_URL)
        form.addRow("接口地址", self.base_edit)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(MODEL_CHOICES)

        if self.config.model and self.config.model not in MODEL_CHOICES:
            self.model_combo.addItem(self.config.model)

        self.model_combo.setCurrentText(self.config.model)
        form.addRow("模型", self.model_combo)

        model_hint = QLabel("deepseek-chat：快、日常运维够用；deepseek-reasoner：复杂排查更强但更慢")
        model_hint.setWordWrap(True)
        model_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        form.addRow("", model_hint)

        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setValue(float(self.config.temperature))
        form.addRow("温度", self.temperature_spin)

        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(128, 8192)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(int(self.config.max_tokens))
        form.addRow("最大回复长度", self.max_tokens_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(10, 600)
        self.timeout_spin.setValue(int(self.config.timeout))
        form.addRow("超时（秒）", self.timeout_spin)

        self.tail_spin = QSpinBox()
        self.tail_spin.setRange(0, 500)
        self.tail_spin.setSingleStep(20)
        self.tail_spin.setValue(int(self.config.include_terminal_tail))
        form.addRow("附带终端输出行数", self.tail_spin)

        self.auto_analyze = QCheckBox("执行命令后自动把输出交给 AI 继续分析")
        self.auto_analyze.setChecked(bool(self.config.auto_analyze_result))
        form.addRow("", self.auto_analyze)

        self.extra_edit = QPlainTextEdit(self.config.system_extra)
        self.extra_edit.setPlaceholderText("例如：回答问题尽量短；命令里不要用 sudo；优先用 yum")
        self.extra_edit.setFixedHeight(64)
        form.addRow("额外要求", self.extra_edit)

        layout.addLayout(form)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self.test_connection)
        self.clear_key_button = QPushButton("清除 Key")
        self.clear_key_button.clicked.connect(self.clear_key)

        buttons.addWidget(self.test_button)
        buttons.addWidget(self.clear_key_button)
        buttons.addStretch()

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)

        env_key = env_api_key()

        if env_key and not stored:
            self.status.setText("检测到环境变量里的 API Key（优先级：界面保存的 Key > 环境变量）")
            self.status.setStyleSheet("color: #3fb950;")

    # ------------------------------------------------------------------

    def _paste_key(self) -> None:
        from PySide6.QtGui import QGuiApplication

        text = (QGuiApplication.clipboard().text() or "").strip()

        if text:
            self.key_edit.setText(text)

    def _build_config(self) -> AiConfig:
        return AiConfig(
            base_url=self.base_edit.text().strip() or DEFAULT_BASE_URL,
            model=self.model_combo.currentText().strip() or "deepseek-chat",
            temperature=float(self.temperature_spin.value()),
            max_tokens=int(self.max_tokens_spin.value()),
            timeout=float(self.timeout_spin.value()),
            include_terminal_tail=int(self.tail_spin.value()),
            auto_analyze_result=self.auto_analyze.isChecked(),
            system_extra=self.extra_edit.toPlainText().strip(),
        )

    # ------------------------------------------------------------------

    def test_connection(self) -> None:
        key = self.key_edit.text().strip() or env_api_key() or ""

        if not key:
            self._set_status("请先填写 API Key", "#f85149")
            return

        from ..ai.client import DeepSeekClient

        client = DeepSeekClient(key, self._build_config())
        self.test_button.setEnabled(False)
        self._set_status("正在连接 …", "#d29922")

        worker = AiTestWorker(client, self)
        worker.ok.connect(lambda reply: self._test_done(True, reply))
        worker.failed.connect(lambda message: self._test_done(False, message))
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()

    def _test_done(self, ok: bool, message: str) -> None:
        self.test_button.setEnabled(True)
        self._test_worker = None

        if ok:
            self._set_status(f"✔ 连接成功（{self.model_combo.currentText()}）：{message[:120]}", "#3fb950")
        else:
            self._set_status(f"✖ {message}", "#f85149")

    def clear_key(self) -> None:
        self.key_edit.clear()
        self._saved_key = ""
        self.credentials.delete(CREDENTIAL_KEY)
        self._set_status("已删除保存的 API Key（环境变量仍然可用）", "#d29922")

    def save(self) -> None:
        self.config = self._build_config()

        try:
            self.config.save()
        except OSError as error:
            QMessageBox.warning(self, "保存失败", f"写入配置失败：{error}")
            return

        key = self.key_edit.text().strip()

        if key:
            self.credentials.set(CREDENTIAL_KEY, key)
        elif self._saved_key:
            self.credentials.delete(CREDENTIAL_KEY)

        self._set_status("已保存", "#3fb950")
        self.accept()

    # ------------------------------------------------------------------

    def _set_status(self, text: str, color: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")
