"""主窗口：左侧主机列表 + 右侧多标签终端。"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QWidget,
)

from .. import APP_NAME, __version__
from ..credentials import CredentialStore
from ..hosts import Host, HostStore
from ..sshclient import SSHClient
from .forward_dialog import ForwardDialog
from .host_dialog import HostDialog
from .session_tab import SessionTab
from .sftp_panel import SftpPanel

STATE_ROLE = Qt.ItemDataRole.UserRole


@dataclass
class AppContext:
    """应用级共享状态。"""

    hosts: HostStore
    credentials: CredentialStore
    jump_clients: dict[str, SSHClient] = field(default_factory=dict)

    def close(self) -> None:
        for client in self.jump_clients.values():
            client.close()

        self.jump_clients.clear()


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)

        self.context = context
        self.tabs: dict[str, SessionTab] = {}
        self.sftp_tabs: dict[str, SftpPanel] = {}
        self._quitting = False

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1180, 720)
        self.setStyleSheet(STYLE)

        self._build_actions()
        self._build_ui()
        self.refresh_hosts()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_actions(self) -> None:
        self.action_new = QAction("新建主机", self)
        self.action_new.setShortcut(QKeySequence("Ctrl+N"))
        self.action_new.triggered.connect(self.new_host)

        self.action_edit = QAction("编辑主机", self)
        self.action_edit.triggered.connect(self.edit_host)

        self.action_delete = QAction("删除主机", self)
        self.action_delete.triggered.connect(self.delete_host)

        self.action_connect = QAction("连接", self)
        self.action_connect.setShortcut(QKeySequence("Ctrl+Return"))
        self.action_connect.triggered.connect(self.connect_selected)

        self.action_reconnect = QAction("重新连接", self)
        self.action_reconnect.setShortcut(QKeySequence("Ctrl+R"))
        self.action_reconnect.triggered.connect(self.reconnect_current)

        self.action_sftp = QAction("SFTP 文件", self)
        self.action_sftp.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.action_sftp.triggered.connect(self.open_sftp)

        self.action_forwards = QAction("端口转发", self)
        self.action_forwards.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.action_forwards.triggered.connect(self.open_forwards)

        self.action_close_tab = QAction("关闭标签", self)
        self.action_close_tab.setShortcut(QKeySequence("Ctrl+W"))
        self.action_close_tab.triggered.connect(self.close_current_tab)

        self.action_copy = QAction("复制", self)
        self.action_copy.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.action_copy.triggered.connect(self.copy_from_terminal)

        self.action_paste = QAction("粘贴", self)
        self.action_paste.setShortcut(QKeySequence("Ctrl+Shift+V"))
        self.action_paste.triggered.connect(self.paste_to_terminal)

        self.action_quit = QAction("退出", self)
        self.action_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.action_quit.triggered.connect(self.close)

    def _build_ui(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.addAction(self.action_connect)
        toolbar.addAction(self.action_reconnect)
        toolbar.addSeparator()
        toolbar.addAction(self.action_sftp)
        toolbar.addAction(self.action_forwards)
        toolbar.addSeparator()
        toolbar.addAction(self.action_new)
        toolbar.addAction(self.action_edit)
        toolbar.addAction(self.action_delete)
        self.addToolBar(toolbar)

        menu = self.menuBar().addMenu("会话")
        menu.addAction(self.action_connect)
        menu.addAction(self.action_reconnect)
        menu.addSeparator()
        menu.addAction(self.action_sftp)
        menu.addAction(self.action_forwards)
        menu.addSeparator()
        menu.addAction(self.action_new)
        menu.addAction(self.action_edit)
        menu.addAction(self.action_delete)
        menu.addSeparator()
        menu.addAction(self.action_close_tab)
        menu.addAction(self.action_quit)

        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addAction(self.action_copy)
        edit_menu.addAction(self.action_paste)

        self.host_list = QListWidget()
        self.host_list.setMinimumWidth(220)
        self.host_list.itemDoubleClicked.connect(lambda _item: self.connect_selected())
        self.host_list.currentItemChanged.connect(self._on_host_selected)

        self.tabs_widget = QTabWidget()
        self.tabs_widget.setTabsClosable(True)
        self.tabs_widget.setMovable(True)
        self.tabs_widget.setDocumentMode(True)
        self.tabs_widget.tabCloseRequested.connect(self.close_tab)
        self.tabs_widget.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.host_list)
        splitter.addWidget(self.tabs_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 940])

        self.setCentralWidget(splitter)
        self.statusBar().showMessage("就绪")

    # ------------------------------------------------------------------
    # 主机列表
    # ------------------------------------------------------------------

    def refresh_hosts(self) -> None:
        selected_id = None
        current = self.host_list.currentItem()

        if current is not None:
            selected_id = current.data(STATE_ROLE)

        self.host_list.clear()

        for host in self.context.hosts.sorted_hosts():
            item = QListWidgetItem(f"{host.display_name}\n{host.target}")
            item.setData(STATE_ROLE, host.id)
            item.setToolTip(f"{host.username}@{host.hostname}:{host.port}")
            self.host_list.addItem(item)

            if host.id == selected_id:
                self.host_list.setCurrentItem(item)

        if self.host_list.currentItem() is None and self.host_list.count():
            self.host_list.setCurrentRow(0)

        self._update_window_title()

    def _selected_host(self) -> Host | None:
        item = self.host_list.currentItem()

        if item is None:
            return None

        return self.context.hosts.get(item.data(STATE_ROLE))

    def _on_host_selected(self, *_args) -> None:
        host = self._selected_host()

        if host is None:
            self.statusBar().showMessage("未选择主机")
            return

        self.statusBar().showMessage(f"{host.display_name} · {host.target} · 认证: {host.auth_type}")

    def new_host(self) -> None:
        dialog = HostDialog(parent=self)

        if dialog.exec() != HostDialog.DialogCode.Accepted:
            return

        host, password, changed = dialog.result_data()
        self.context.hosts.add(host)

        if changed and password:
            self.context.credentials.set(host.id, password)

        self.refresh_hosts()
        self._select_host(host.id)
        self.statusBar().showMessage(f"已保存主机 {host.display_name}")

    def edit_host(self) -> None:
        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        original_id = host.id
        known = self.context.credentials.get(host.id)
        dialog = HostDialog(host=host, known_password=known, parent=self)

        if dialog.exec() != HostDialog.DialogCode.Accepted:
            return

        updated, password, changed = dialog.result_data()
        self.context.hosts.update(updated)

        if updated.id != original_id:
            self.context.credentials.rename(original_id, updated.id)

        if changed:
            if password:
                self.context.credentials.set(updated.id, password)
            else:
                self.context.credentials.delete(updated.id)

        self.refresh_hosts()
        self._select_host(updated.id)
        self.statusBar().showMessage(f"已更新主机 {updated.display_name}")

    def delete_host(self) -> None:
        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        confirm = QMessageBox.question(
            self,
            APP_NAME,
            f"删除主机「{host.display_name}」及其保存的密码？",
        )

        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.context.credentials.delete(host.id)
        self.context.hosts.remove(host.id)
        self.refresh_hosts()
        self.statusBar().showMessage(f"已删除主机 {host.display_name}")

    def _select_host(self, host_id: str) -> None:
        for row in range(self.host_list.count()):
            item = self.host_list.item(row)

            if item.data(STATE_ROLE) == host_id:
                self.host_list.setCurrentItem(item)
                return

    # ------------------------------------------------------------------
    # 连接
    # ------------------------------------------------------------------

    def connect_selected(self) -> None:
        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        self.connect_host(host)

    def connect_host(self, host: Host) -> None:
        if host.id in self.tabs:
            self.tabs_widget.setCurrentWidget(self.tabs[host.id])
            return

        credential = self._ensure_credential(host)

        if credential is None:
            return

        jump = self._jump_client_for(host)

        tab = SessionTab(host, credential=credential, jump=jump)
        tab.error_raised.connect(lambda message, h=host: self._on_session_error(h, message))
        tab.title_changed.connect(lambda title, h=host: self._on_title(h, title))
        tab.closed.connect(lambda h=host: self._on_session_finished(h))

        index = self.tabs_widget.addTab(tab, host.display_name)
        self.tabs_widget.setTabToolTip(index, host.target)
        self.tabs_widget.setCurrentIndex(index)

        self.tabs[host.id] = tab
        self.context.hosts.mark_connected(host.id)
        self._update_window_title()

    def _ensure_credential(self, host: Host) -> str | None:
        """取主机凭据；密码认证且没有保存过时，向用户询问一次并加密保存。"""

        credential = self.context.credentials.get(host.id)

        if host.auth_type != "password" or credential:
            return credential

        text, ok = QInputDialog.getText(
            self,
            APP_NAME,
            f"请输入 {host.target} 的密码（将加密保存到本机）：",
            QInputDialog.EchoMode.Normal,
        )

        if not ok:
            return None

        if text:
            self.context.credentials.set(host.id, text)

        return text or None

    # ------------------------------------------------------------------
    # SFTP / 端口转发
    # ------------------------------------------------------------------

    def open_sftp(self) -> None:
        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        panel = self.sftp_tabs.get(host.id)

        if panel is not None:
            self.tabs_widget.setCurrentWidget(panel)
            return

        credential = self._ensure_credential(host)

        if credential is None:
            return

        panel = SftpPanel(host, credential=credential, jump=self._jump_client_for(host))
        index = self.tabs_widget.addTab(panel, f"SFTP · {host.display_name}")
        self.tabs_widget.setTabToolTip(index, host.target)
        self.tabs_widget.setCurrentIndex(index)

        self.sftp_tabs[host.id] = panel
        self._update_window_title()

    def open_forwards(self) -> None:
        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        credential = self._ensure_credential(host)

        if credential is None:
            return

        session = self.tabs.get(host.id)
        existing = session.session.client if session is not None else None

        dialog = ForwardDialog(
            host,
            credential=credential,
            jump=self._jump_client_for(host),
            existing_client=existing,
            parent=self,
        )
        dialog.exec()
        self.statusBar().showMessage(f"{host.display_name} 端口转发已关闭")

    def _jump_client_for(self, host: Host) -> SSHClient | None:
        """按需建立并缓存跳板机连接。"""

        if not host.jump_host_id:
            return None

        cached = self.context.jump_clients.get(host.jump_host_id)

        if cached is not None and cached.connected:
            return cached

        jump_host = self.context.hosts.get(host.jump_host_id)

        if jump_host is None:
            return None

        jump_credential = self.context.credentials.get(jump_host.id)
        client = SSHClient(jump_host)

        try:
            client.connect(
                hostname=jump_host.hostname,
                port=jump_host.port,
                username=jump_host.username,
                password=jump_credential if jump_host.auth_type == "password" else None,
                key_path=jump_host.key_path if jump_host.auth_type == "key" else None,
                passphrase=jump_credential if jump_host.auth_type == "key" else None,
                verify_host_key=jump_host.verify_host_key,
            )
        except Exception as error:  # noqa: BLE001
            self.statusBar().showMessage(f"跳板机连接失败：{error}")
            return None

        self.context.jump_clients[host.jump_host_id] = client

        return client

    def _on_session_error(self, host: Host, message: str) -> None:
        self.statusBar().showMessage(f"{host.display_name} 连接失败：{message.splitlines()[0]}")

        if "未知主机" not in message:
            return

        answer = QMessageBox.warning(
            self,
            APP_NAME,
            f"{message}\n\n是否信任该服务器并重试？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if answer != QMessageBox.StandardButton.Yes:
            return

        host.verify_host_key = False
        self.context.hosts.update(host)

        tab = self.tabs.get(host.id)

        if tab is not None:
            tab.reconnect()

    def _on_title(self, host: Host, title: str) -> None:
        tab = self.tabs.get(host.id)

        if tab is None:
            return

        index = self.tabs_widget.indexOf(tab)

        if index >= 0 and title:
            self.tabs_widget.setTabText(index, title[:40])

    def _on_session_finished(self, host: Host) -> None:
        if self._quitting:
            return

        self.statusBar().showMessage(f"{host.display_name} 会话已结束")

    def _on_tab_changed(self, index: int) -> None:
        widget = self.tabs_widget.widget(index)

        if isinstance(widget, (SessionTab, SftpPanel)):
            self._select_host(widget.host.id)

            if isinstance(widget, SessionTab):
                widget.view.setFocus()

        self._update_window_title()

    # ------------------------------------------------------------------
    # 标签管理
    # ------------------------------------------------------------------

    def close_current_tab(self) -> None:
        self.close_tab(self.tabs_widget.currentIndex())

    def close_tab(self, index: int) -> None:
        widget = self.tabs_widget.widget(index)

        if isinstance(widget, SessionTab):
            host = widget.host
            widget.close()
            self.tabs_widget.removeTab(index)

            if self.tabs.get(host.id) is widget:
                del self.tabs[host.id]
        elif isinstance(widget, SftpPanel):
            host = widget.host
            widget.close()
            self.tabs_widget.removeTab(index)

            if self.sftp_tabs.get(host.id) is widget:
                del self.sftp_tabs[host.id]
        else:
            return

        self._update_window_title()

    def reconnect_current(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SftpPanel):
            self.statusBar().showMessage("SFTP 标签不支持重连，请关闭后重新打开")
            return

        if not isinstance(widget, SessionTab):
            self.statusBar().showMessage("当前没有活动会话")
            return

        jump = self._jump_client_for(widget.host)
        credential = self._ensure_credential(widget.host)
        widget.reconnect(credential=credential, jump=jump)

    # ------------------------------------------------------------------
    # 剪贴板
    # ------------------------------------------------------------------

    def copy_from_terminal(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            widget.view.copy()

    def paste_to_terminal(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            widget.view.paste()

    # ------------------------------------------------------------------

    def _update_window_title(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            self.setWindowTitle(f"{APP_NAME} {__version__} — {widget.display_name}")
        elif isinstance(widget, SftpPanel):
            self.setWindowTitle(f"{APP_NAME} {__version__} — SFTP {widget.host.display_name}")
        else:
            self.setWindowTitle(f"{APP_NAME} {__version__}")

    def closeEvent(self, event):  # noqa: N802
        self._quitting = True

        for tab in list(self.tabs.values()):
            tab.close()

        for panel in list(self.sftp_tabs.values()):
            panel.close()

        self.tabs.clear()
        self.sftp_tabs.clear()
        self.context.close()

        event.accept()


STYLE = """
QMainWindow, QWidget { background-color: #0d1117; color: #e6edf3; }
QListWidget {
    background-color: #161b22;
    color: #e6edf3;
    border: none;
    padding: 6px;
}
QListWidget::item { padding: 8px; border-radius: 6px; }
QListWidget::item:selected { background-color: #1f6feb; color: #ffffff; }
QListWidget::item:hover { background-color: #21262d; }
QTabWidget::pane { border: none; background-color: #0d1117; }
QTabBar::tab {
    background-color: #161b22;
    color: #8b949e;
    padding: 8px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected { background-color: #21262d; color: #e6edf3; }
QToolBar { background-color: #161b22; border: none; spacing: 6px; padding: 4px; }
QToolButton { color: #e6edf3; padding: 6px 10px; border-radius: 6px; }
QToolButton:hover { background-color: #30363d; }
QMenuBar { background-color: #161b22; color: #e6edf3; }
QMenuBar::item:selected { background-color: #30363d; }
QMenu { background-color: #161b22; color: #e6edf3; }
QMenu::item:selected { background-color: #1f6feb; }
QStatusBar { background-color: #161b22; color: #8b949e; }
QSplitter::handle { background-color: #21262d; }
QLabel { color: #e6edf3; }
QLineEdit, QSpinBox, QComboBox {
    background-color: #21262d;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px;
}
QPushButton {
    background-color: #21262d;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px 12px;
}
QPushButton:hover { background-color: #30363d; }
QTreeView, QTableWidget {
    background-color: #0d1117;
    color: #e6edf3;
    border: 1px solid #21262d;
    alternate-background-color: #11161d;
    gridline-color: #21262d;
}
QTreeView::item:selected, QTableWidget::item:selected { background-color: #1f6feb; color: #ffffff; }
QHeaderView::section {
    background-color: #161b22;
    color: #8b949e;
    border: none;
    padding: 4px 6px;
}
QProgressBar {
    background-color: #21262d;
    border: none;
    border-radius: 4px;
    color: #e6edf3;
    text-align: center;
}
QProgressBar::chunk { background-color: #1f6feb; border-radius: 4px; }
"""
