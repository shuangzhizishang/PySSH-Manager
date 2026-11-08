"""AI 用量与花费：按模型列出 token、缓存命中率和估算金额，并可查余额。

价格按官方价目表（元 / 百万 tokens）估算：空闲时段是高峰价的一半，周末全天
按空闲价。界面上的金额是估算值，实际以 DeepSeek 账单为准。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..ai import peak_note
from ..ai.pricing import band_name, format_cny, format_percent, format_tokens, model_label
from ..ai.usage import Stats, UsageLedger
from ..ai.worker import AiBalanceWorker

COLUMNS = ["模型", "次数", "输入", "缓存命中", "命中率", "输出", "花费"]


class AiUsageDialog(QDialog):
    """用量明细 + 余额查询 + 清零。"""

    def __init__(self, ledger: UsageLedger, client_factory, parent=None) -> None:
        super().__init__(parent)

        self.ledger = ledger
        self._client_factory = client_factory
        self._balance_worker: AiBalanceWorker | None = None
        self.reset_done = False

        self.setWindowTitle("AI 用量与花费")
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        hint = QLabel(
            "价格按官方价目表估算（元 / 百万 tokens）：输入命中 / 未命中 / 输出分档计价。"
            f"{peak_note()}。法定节假日无法离线判断，误差很小。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8b949e; font-size: 11px;")
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for index in range(1, len(COLUMNS)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)

        self.table.setMinimumHeight(220)
        layout.addWidget(self.table, 1)

        self.balance_label = QLabel("余额：点『查余额』（需要 API Key）")
        self.balance_label.setWordWrap(True)
        self.balance_label.setStyleSheet("color: #8b949e;")
        layout.addWidget(self.balance_label)

        buttons = QHBoxLayout()

        self.refresh_button = QPushButton("查余额")
        self.refresh_button.setToolTip("GET /user/balance，查账户剩余额度（不消耗 tokens）")
        self.refresh_button.clicked.connect(self.refresh_balance)

        self.models_button = QPushButton("刷新模型列表")
        self.models_button.setToolTip("GET /models，把官方最新模型同步到面板下拉框")
        self.models_button.clicked.connect(self._refresh_models)

        reset_button = QPushButton("清零统计")
        reset_button.setToolTip("把累计 token 与金额清零（不影响账户余额）")
        reset_button.clicked.connect(self.reset_stats)

        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.models_button)
        buttons.addWidget(reset_button)
        buttons.addStretch()

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        close_button = box.button(QDialogButtonBox.StandardButton.Close)

        if close_button is not None:
            close_button.setText("关闭")

        buttons.addWidget(box)
        layout.addLayout(buttons)

        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        total = self.ledger.total
        self.summary_label.setText(
            f"累计：{total.line()}　　本次：{self.ledger.session.line() if self.ledger.session.requests else '还没用过'}"
            f"　　当前：{band_name() == 'peak' and '高峰价' or '空闲价'}"
        )

        rows = self.ledger.rows()
        self.table.setRowCount(len(rows) + 1)

        for row, (model, stats) in enumerate(rows):
            self._fill_row(row, model, stats)

        self._fill_row(len(rows), "合计", total, is_total=True)
        self.table.resizeRowsToContents()

    def _fill_row(self, row: int, model: str, stats: Stats, is_total: bool = False) -> None:
        values = [
            "合计" if is_total else f"{model_label(model)}（{model}）",
            str(stats.requests),
            format_tokens(stats.prompt_tokens),
            format_tokens(stats.cached_tokens),
            format_percent(stats.cached_tokens, stats.prompt_tokens),
            format_tokens(stats.completion_tokens),
            format_cny(stats.cost_cny),
        ]

        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(
                f"输入 {stats.prompt_tokens:,} tok\n缓存命中 {stats.cached_tokens:,} tok\n"
                f"输出 {stats.completion_tokens:,} tok（其中思维链 {stats.reasoning_tokens:,}）\n"
                f"估算花费 {format_cny(stats.cost_cny)}"
            )

            if column > 0:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )

            if is_total:
                font = item.font()
                font.setBold(True)
                item.setFont(font)

            self.table.setItem(row, column, item)

    # ------------------------------------------------------------------

    def refresh_balance(self) -> None:
        client = self._client_factory()

        if client is None:
            self.balance_label.setText("余额：还没有配置 API Key")
            self.balance_label.setStyleSheet("color: #d29922;")
            return

        if self._balance_worker is not None:
            return

        self.refresh_button.setEnabled(False)
        self.balance_label.setText("余额：查询中 …")
        self.balance_label.setStyleSheet("color: #8b949e;")

        worker = AiBalanceWorker(client, self)
        worker.ok.connect(self._on_balance)
        worker.failed.connect(self._on_balance_failed)
        worker.finished.connect(worker.deleteLater)
        self._balance_worker = worker
        worker.start()

    def _on_balance(self, info: dict) -> None:
        self._balance_worker = None
        self.refresh_button.setEnabled(True)

        infos = info.get("infos") or []
        parts = []

        for entry in infos:
            currency = entry.get("currency", "CNY")
            symbol = "¥" if currency == "CNY" else "$"
            parts.append(
                f"{symbol}{entry.get('total', '0')}（赠金 {symbol}{entry.get('granted', '0')}"
                f" · 充值 {symbol}{entry.get('topped_up', '0')}）"
            )

        available = info.get("is_available")
        head = "余额充足" if available else "余额不足，请充值"

        self.balance_label.setText(f"余额：{'；'.join(parts) or '（接口未返回金额）'} · {head}")
        self.balance_label.setStyleSheet(
            "color: #3fb950;" if available else "color: #f85149;"
        )

    def _on_balance_failed(self, message: str) -> None:
        self._balance_worker = None
        self.refresh_button.setEnabled(True)
        self.balance_label.setText(f"余额查询失败：{message}")
        self.balance_label.setStyleSheet("color: #f85149;")

    def _refresh_models(self) -> None:
        """让面板把官方模型列表拉一遍（面板自己会弹提示）。"""

        parent = self.parent()

        if parent is not None and hasattr(parent, "reload_models"):
            parent.reload_models(silent=True)

    def reset_stats(self) -> None:
        reply = QMessageBox.question(
            self,
            "清零统计",
            "确定把累计的 token 用量与估算金额清零吗？（不影响账户余额）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self.ledger.reset()
        self.reset_done = True
        self.refresh()

    # ------------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt 命名
        if self._balance_worker is not None:
            self._balance_worker.wait(1500)
            self._balance_worker = None

        super().closeEvent(event)
