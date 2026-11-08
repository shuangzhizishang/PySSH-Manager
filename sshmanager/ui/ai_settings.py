"""AI 助手设置：API Key、模型、思考强度、接口地址、价格表与行为开关。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..ai import (
    AiConfig,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    EFFORT_CHOICES,
    EFFORT_LABELS,
    MODEL_CHOICES,
    env_api_key,
    model_label,
)
from ..ai.config import CREDENTIAL_KEY
from ..ai.pricing import canonical_model, default_prices
from ..ai.worker import AiModelsWorker, AiTestWorker
from ..credentials import CredentialStore

PRICE_COLUMNS = ["模型", "空闲·命中", "空闲·未命中", "空闲·输出", "高峰·命中", "高峰·未命中", "高峰·输出"]


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
        self._models_worker: AiModelsWorker | None = None
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

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self._fill_model_combo(MODEL_CHOICES)

        self.refresh_models_button = QPushButton("刷新官方列表")
        self.refresh_models_button.setToolTip("调用 GET /models 拉取当前可用的模型")
        self.refresh_models_button.clicked.connect(self.refresh_models)

        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.refresh_models_button)
        form.addRow("模型", model_row)

        model_hint = QLabel(
            "deepseek-flash = DeepSeek-V4.1-Flash（默认，快且便宜，1M 上下文）；"
            "deepseek-v4-pro = DeepSeek-V4-Pro（更强更贵）。旧名 deepseek-chat / deepseek-reasoner 已停用。"
        )
        model_hint.setWordWrap(True)
        model_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        form.addRow("", model_hint)

        self.effort_combo = QComboBox()

        for key in EFFORT_CHOICES:
            self.effort_combo.addItem(EFFORT_LABELS.get(key, key), key)

        index = self.effort_combo.findData(self.config.effort)
        self.effort_combo.setCurrentIndex(index if index >= 0 else 2)
        form.addRow("思考强度", self.effort_combo)

        effort_hint = QLabel(
            "思考模式默认开启（effort=high），会先输出思维链再给答案；关闭思考更快但复杂问题略差。"
            "思考模式下温度参数不生效。"
        )
        effort_hint.setWordWrap(True)
        effort_hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        form.addRow("", effort_hint)

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

        self.peak_pricing = QCheckBox("按峰谷时段估算花费（空闲时段为高峰价的一半）")
        self.peak_pricing.setChecked(bool(self.config.peak_pricing))
        form.addRow("", self.peak_pricing)

        self.price_table = QTableWidget(0, len(PRICE_COLUMNS))
        self.price_table.setHorizontalHeaderLabels(PRICE_COLUMNS)
        self.price_table.verticalHeader().setVisible(False)
        self.price_table.setFixedHeight(118)
        header = self.price_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for column in range(1, len(PRICE_COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)

        self._fill_price_table()

        price_row = QHBoxLayout()
        price_row.addWidget(self.price_table, 1)

        restore_button = QPushButton("恢复默认价")
        restore_button.setToolTip("把价格表恢复成官方价目表")
        restore_button.clicked.connect(self._restore_prices)
        price_row.addWidget(restore_button)
        form.addRow("价格表（元/百万 tokens）", price_row)

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
        save_button = box.button(QDialogButtonBox.StandardButton.Save)
        cancel_button = box.button(QDialogButtonBox.StandardButton.Cancel)

        if save_button is not None:
            save_button.setText("保存")

        if cancel_button is not None:
            cancel_button.setText("取消")

        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)

        env_key = env_api_key()

        if env_key and not stored:
            self.status.setText("检测到环境变量里的 API Key（优先级：界面保存的 Key > 环境变量）")
            self.status.setStyleSheet("color: #3fb950;")

    # ------------------------------------------------------------------
    # 模型列表 / 价格表
    # ------------------------------------------------------------------

    def _current_model(self) -> str:
        text = (self.model_combo.currentText() or "").strip()
        index = self.model_combo.currentIndex()
        data = self.model_combo.itemData(index) if index >= 0 else None

        if data and text == (self.model_combo.itemText(index) or ""):
            return str(data)

        return canonical_model(text) or text

    def _fill_model_combo(self, models) -> None:
        """用模型列表填充下拉框；``models`` 可以是 id 字符串或 /models 的 dict。"""

        current = (self.config.model or DEFAULT_MODEL).strip()
        entries: list[str] = []

        for item in models or []:
            if isinstance(item, dict):
                model_id = str(item.get("id") or "").strip()
            else:
                model_id = str(item or "").strip()

            if model_id and model_id not in entries:
                entries.append(model_id)

        if current and current not in entries:
            entries.insert(0, current)

        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        for model_id in entries:
            self.model_combo.addItem(model_label(model_id), model_id)
            self.model_combo.setItemData(
                self.model_combo.count() - 1, model_id, 3  # Qt.ToolTipRole
            )

        index = self.model_combo.findData(current)

        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        else:
            self.model_combo.setEditText(current)

        self.model_combo.blockSignals(False)

    def refresh_models(self) -> None:
        """``GET /models`` 拉官方模型列表。"""

        key = self.key_edit.text().strip() or env_api_key() or ""

        if not key:
            self._set_status("请先填写 API Key", "#f85149")
            return

        if self._models_worker is not None:
            return

        from ..ai.client import DeepSeekClient

        client = DeepSeekClient(key, self._build_config())
        self.refresh_models_button.setEnabled(False)
        self._set_status("正在获取官方模型列表 …", "#d29922")

        worker = AiModelsWorker(client, self)
        worker.ok.connect(self._on_models_ready)
        worker.failed.connect(self._on_models_failed)
        worker.finished.connect(worker.deleteLater)
        self._models_worker = worker
        worker.start()

    def _on_models_ready(self, models: list) -> None:
        self._models_worker = None
        self.refresh_models_button.setEnabled(True)

        if not models:
            self._set_status("接口没有返回模型列表", "#d29922")
            return

        self._fill_model_combo(models)
        names = "、".join(str(item.get("id")) for item in models if isinstance(item, dict))
        self._set_status(f"已获取 {len(models)} 个模型：{names[:200]}", "#3fb950")

    def _on_models_failed(self, message: str) -> None:
        self._models_worker = None
        self.refresh_models_button.setEnabled(True)
        self._set_status(f"获取模型列表失败：{message}", "#f85149")

    def _fill_price_table(self, prices: dict | None = None) -> None:
        from ..ai.pricing import ModelPrice

        table = prices if prices is not None else self.config.price_table()
        order = ["deepseek-flash", "deepseek-v4-pro"]
        names = [name for name in order if name in table] + [
            name for name in table if name not in order
        ]

        self.price_table.setRowCount(0)

        for model in names:
            entry = table.get(model) or {}
            idle = ModelPrice.from_dict(entry.get("idle"))
            peak = ModelPrice.from_dict(entry.get("peak"))

            row = self.price_table.rowCount()
            self.price_table.insertRow(row)

            name_item = QTableWidgetItem(model)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(model_label(model))
            self.price_table.setItem(row, 0, name_item)

            values = [idle.hit, idle.miss, idle.output, peak.hit, peak.miss, peak.output]

            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(f"{value:g}")
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self.price_table.setItem(row, column, item)

    def _read_price_table(self) -> dict:
        prices: dict = {}

        for row in range(self.price_table.rowCount()):
            name_item = self.price_table.item(row, 0)
            model = canonical_model(name_item.text() if name_item else "")

            if not model:
                continue

            values: list[float] = []

            for column in range(1, len(PRICE_COLUMNS)):
                item = self.price_table.item(row, column)

                try:
                    values.append(max(0.0, float((item.text() if item else "").strip())))
                except ValueError:
                    values.append(0.0)

            while len(values) < 6:
                values.append(0.0)

            prices[model] = {
                "idle": {"hit": values[0], "miss": values[1], "output": values[2]},
                "peak": {"hit": values[3], "miss": values[4], "output": values[5]},
            }

        return prices

    def _restore_prices(self) -> None:
        self._fill_price_table(default_prices())
        self._set_status("价格表已恢复为官方价目表（保存后生效）", "#3fb950")

    # ------------------------------------------------------------------

    def _paste_key(self) -> None:
        from PySide6.QtGui import QGuiApplication

        text = (QGuiApplication.clipboard().text() or "").strip()

        if text:
            self.key_edit.setText(text)

    def _build_config(self) -> AiConfig:
        return AiConfig(
            base_url=self.base_edit.text().strip() or DEFAULT_BASE_URL,
            model=self._current_model() or DEFAULT_MODEL,
            temperature=float(self.temperature_spin.value()),
            max_tokens=int(self.max_tokens_spin.value()),
            timeout=float(self.timeout_spin.value()),
            effort=str(self.effort_combo.currentData() or "high"),
            peak_pricing=self.peak_pricing.isChecked(),
            prices=self._read_price_table(),
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
