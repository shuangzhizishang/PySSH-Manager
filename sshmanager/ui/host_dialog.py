"""新建 / 编辑主机对话框。"""

from __future__ import annotations

import uuid

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hosts import Host
from ..sshclient import save_host_key


class HostDialog(QDialog):
    """编辑一条主机配置。

    ``result_data()`` 返回 ``(host, password, password_changed)``：
    ``password_changed`` 为 False 表示用户没改密码，调用方应保留原凭据。
    """

    def __init__(
        self,
        host: Host | None = None,
        known_password: str | None = None,
        hosts_for_jump: list[Host] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.setWindowTitle("编辑主机" if host else "新建主机")
        self.setMinimumWidth(480)

        self._host = host or Host()
        self._has_known_password = known_password is not None
        self._jump_candidates = hosts_for_jump or []

        form = QFormLayout()

        self.label_edit = QLineEdit(self._host.label)
        self.label_edit.setPlaceholderText("例如：测试机 / 生产环境")
        form.addRow("名称", self.label_edit)

        self.hostname_edit = QLineEdit(self._host.hostname)
        self.hostname_edit.setPlaceholderText("IP 或域名")
        form.addRow("地址", self.hostname_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(self._host.port or 22))
        form.addRow("端口", self.port_spin)

        self.username_edit = QLineEdit(self._host.username)
        form.addRow("用户名", self.username_edit)

        self.auth_combo = QComboBox()
        self.auth_combo.addItem("密码", "password")
        self.auth_combo.addItem("私钥文件", "key")
        index = self.auth_combo.findData(self._host.auth_type)
        self.auth_combo.setCurrentIndex(max(0, index))
        self.auth_combo.currentIndexChanged.connect(self._sync_auth_fields)
        form.addRow("认证方式", self.auth_combo)

        # 密码
        self.password_label = QLabel("密码")
        self.password_edit = QLineEdit(known_password or "")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText(
            "留空则保留已保存的密码" if self._has_known_password else "登录密码"
        )
        form.addRow(self.password_label, self.password_edit)

        # 私钥
        self.key_label = QLabel("私钥")
        self.key_edit = QLineEdit(self._host.key_path)
        self.key_edit.setPlaceholderText("私钥文件路径，如 C:/Users/me/.ssh/id_ed25519")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_key)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(browse)

        self.key_widget = QWidget()
        self.key_widget.setLayout(key_row)
        form.addRow(self.key_label, self.key_widget)

        self.verify_check = QCheckBox("校验服务器指纹（known_hosts）")
        self.verify_check.setChecked(bool(self._host.verify_host_key))
        form.addRow("", self.verify_check)

        self.tags_edit = QLineEdit(", ".join(str(tag) for tag in (self._host.tags or [])))
        self.tags_edit.setPlaceholderText("多个标签用逗号分隔，第一个作为侧边栏分组")
        form.addRow("标签/分组", self.tags_edit)

        self.jump_combo = QComboBox()
        self.jump_combo.addItem("（直连）", "")
        self._build_jump_options()
        form.addRow("跳板机", self.jump_combo)

        trust_row = QHBoxLayout()
        trust_row.setContentsMargins(0, 0, 0, 0)
        trust_button = QPushButton("信任并记住此服务器指纹")
        trust_button.clicked.connect(self._trust_host_key)
        self.trust_status = QLabel("")
        self.trust_status.setStyleSheet("color: #8b949e;")
        self.trust_status.setWordWrap(True)
        trust_row.addWidget(trust_button)
        trust_row.addWidget(self.trust_status, 1)

        trust_widget = QWidget()
        trust_widget.setLayout(trust_row)
        form.addRow("", trust_widget)

        self.term_edit = QLineEdit(self._host.term_type)
        form.addRow("TERM", self.term_edit)

        container = QVBoxLayout(self)
        container.addLayout(form)

        hint = QLabel("密码经 Windows DPAPI 加密后保存在本机，不写入代码、不以明文落盘。")
        hint.setStyleSheet("color: #8b949e;")
        hint.setWordWrap(True)
        container.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        container.addWidget(buttons)

        self._sync_auth_fields()

    # ------------------------------------------------------------------

    def _build_jump_options(self) -> None:
        """列出可以当跳板机的其他主机。"""

        for candidate in self._jump_candidates:
            if candidate.id == self._host.id:
                continue

            self.jump_combo.addItem(
                f"{candidate.display_name}（{candidate.username}@{candidate.hostname}）",
                candidate.id,
            )

        index = self.jump_combo.findData(self._host.jump_host_id)
        self.jump_combo.setCurrentIndex(max(0, index))

    def _sync_auth_fields(self) -> None:
        is_password = self.auth_combo.currentData() == "password"

        self.password_label.setVisible(is_password)
        self.password_edit.setVisible(is_password)
        self.key_label.setVisible(not is_password)
        self.key_widget.setVisible(not is_password)

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥文件")

        if path:
            self.key_edit.setText(path)

    def _trust_host_key(self) -> None:
        hostname = self.hostname_edit.text().strip()

        if not hostname:
            self.trust_status.setText("请先填写地址")
            return

        self.trust_status.setText("正在读取服务器指纹…")
        QApplication.processEvents()

        try:
            fingerprint = save_host_key(hostname, self.port_spin.value())
        except Exception as error:  # noqa: BLE001 - 需要把失败原因显示出来
            self.trust_status.setText(f"失败：{error}")
            return

        self.trust_status.setText(f"已记住 {fingerprint}")
        self.verify_check.setChecked(True)

    def _on_accept(self) -> None:
        if not self.hostname_edit.text().strip():
            self.hostname_edit.setFocus()
            return

        self.accept()

    # ------------------------------------------------------------------

    def result_data(self) -> tuple[Host, str | None, bool]:
        old_id = self._host.id
        host = self._host

        host.label = self.label_edit.text().strip()
        host.hostname = self.hostname_edit.text().strip()
        host.port = int(self.port_spin.value())
        host.username = self.username_edit.text().strip() or "root"
        host.auth_type = self.auth_combo.currentData()
        host.key_path = self.key_edit.text().strip()
        host.verify_host_key = self.verify_check.isChecked()
        host.term_type = self.term_edit.text().strip() or "xterm-256color"
        host.jump_host_id = self.jump_combo.currentData() or ""
        host.tags = [
            tag.strip() for tag in self.tags_edit.text().replace("，", ",").split(",") if tag.strip()
        ]

        if not host.id:
            host.id = uuid.uuid4().hex[:12]

        password = self.password_edit.text()

        if not password and self._has_known_password:
            return host, None, False

        return host, password, True

    @property
    def original_id(self) -> str:
        """未修改前的主机 id，便于调用方迁移凭据。"""

        return self._host.id
