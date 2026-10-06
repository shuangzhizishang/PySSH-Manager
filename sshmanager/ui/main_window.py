"""主窗口：左侧主机列表 + 右侧多标签终端。"""

from __future__ import annotations

from dataclasses import dataclass, field

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
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
from ..hostio import (
    export_json,
    export_openssh_config,
    import_json,
    import_json_credentials,
    parse_openssh_config,
    resolve_proxy_jumps,
)
from ..hosts import Host, HostStore
from ..keygen import KeyStore
from ..localsession import conpty_available
from ..sshclient import SSHClient
from .forward_dialog import ForwardDialog
from .host_dialog import HostDialog
from .key_dialog import KeyManagerDialog
from .replay_dialog import ReplayDialog
from .session_tab import LocalTerminalTab, SessionTab
from .sftp_panel import SftpPanel
from .split_tab import SplitTerminalTab

STATE_ROLE = Qt.ItemDataRole.UserRole


@dataclass
class AppContext:
    """应用级共享状态。"""

    hosts: HostStore
    credentials: CredentialStore
    jump_clients: dict[str, SSHClient] = field(default_factory=dict)
    keys: KeyStore | None = None

    def key_store(self) -> KeyStore:
        if self.keys is None:
            self.keys = KeyStore()

        return self.keys

    def close(self) -> None:
        for client in self.jump_clients.values():
            client.close()

        self.jump_clients.clear()


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext, parent=None) -> None:
        super().__init__(parent)

        self.context = context
        self.tabs: dict[str, SessionTab | SplitTerminalTab] = {}
        self.sftp_tabs: dict[str, SftpPanel] = {}
        self.local_tabs: dict[str, LocalTerminalTab] = {}
        self.replay_dialog = None
        self._quitting = False

        self.record_timer = QTimer(self)
        self.record_timer.setInterval(1000)
        self.record_timer.timeout.connect(self._on_record_tick)

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

        self.action_local = QAction("本地终端", self)
        self.action_local.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.action_local.triggered.connect(self.open_local_terminal)

        self.action_split_h = QAction("左右分屏", self)
        self.action_split_h.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self.action_split_h.triggered.connect(lambda: self.split_current(Qt.Orientation.Horizontal))

        self.action_split_v = QAction("上下分屏", self)
        self.action_split_v.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.action_split_v.triggered.connect(lambda: self.split_current(Qt.Orientation.Vertical))

        self.action_focus_next = QAction("切换分屏焦点", self)
        self.action_focus_next.setShortcut(QKeySequence("Ctrl+Shift+W"))
        self.action_focus_next.triggered.connect(self.focus_next_pane)

        self.action_import = QAction("导入主机…", self)
        self.action_import.triggered.connect(self.import_hosts)

        self.action_export = QAction("导出主机…", self)
        self.action_export.triggered.connect(self.export_hosts)

        self.action_keys = QAction("SSH 密钥管理", self)
        self.action_keys.setShortcut(QKeySequence("Ctrl+Shift+K"))
        self.action_keys.triggered.connect(self.manage_keys)

        self.action_record = QAction("开始录制会话", self)
        self.action_record.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self.action_record.triggered.connect(self.toggle_recording)

        self.action_replay = QAction("回放录制…", self)
        self.action_replay.triggered.connect(self.open_replay)

        self.action_sync = QAction("配置同步…", self)
        self.action_sync.triggered.connect(self.sync_config)

        self.action_import_ppk = QAction("导入 PuTTY 密钥…", self)
        self.action_import_ppk.triggered.connect(self.import_ppk)

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
        toolbar.addAction(self.action_local)
        toolbar.addSeparator()
        toolbar.addAction(self.action_split_h)
        toolbar.addAction(self.action_split_v)
        toolbar.addAction(self.action_focus_next)
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
        menu.addAction(self.action_local)
        menu.addSeparator()
        menu.addAction(self.action_record)
        menu.addAction(self.action_replay)
        menu.addSeparator()
        menu.addAction(self.action_split_h)
        menu.addAction(self.action_split_v)
        menu.addAction(self.action_focus_next)
        menu.addSeparator()
        menu.addAction(self.action_close_tab)
        menu.addAction(self.action_quit)

        hosts_menu = self.menuBar().addMenu("主机")
        hosts_menu.addAction(self.action_new)
        hosts_menu.addAction(self.action_edit)
        hosts_menu.addAction(self.action_delete)
        hosts_menu.addSeparator()
        hosts_menu.addAction(self.action_import)
        hosts_menu.addAction(self.action_export)
        hosts_menu.addSeparator()
        hosts_menu.addAction(self.action_keys)
        hosts_menu.addAction(self.action_import_ppk)
        hosts_menu.addAction(self.action_sync)

        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addAction(self.action_copy)
        edit_menu.addAction(self.action_paste)

        self.host_list = QListWidget()
        self.host_list.setMinimumWidth(220)
        self.host_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.host_list.itemDoubleClicked.connect(lambda _item: self.connect_selected())
        self.host_list.currentItemChanged.connect(self._on_host_selected)
        self.host_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.host_list.customContextMenuRequested.connect(self._show_host_menu)

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

        # 按标签分组显示：有多个分组时插入不可选的分组标题
        grouped: dict[str, list[Host]] = {}

        for host in self.context.hosts.sorted_hosts():
            grouped.setdefault(host.group, []).append(host)

        show_groups = len(grouped) > 1

        for group in sorted(grouped, key=lambda name: (name == "未分组", name.lower())):
            if show_groups:
                header = QListWidgetItem(f"— {group} —")
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setForeground(Qt.GlobalColor.gray)
                self.host_list.addItem(header)

            for host in grouped[group]:
                item = QListWidgetItem(f"{host.display_name}\n{host.target}")
                item.setData(STATE_ROLE, host.id)
                tooltip = f"{host.username}@{host.hostname}:{host.port}"

                if host.tags:
                    tooltip += f"\n标签：{', '.join(str(tag) for tag in host.tags)}"

                if host.jump_host_id:
                    jump = self.context.hosts.get(host.jump_host_id)
                    tooltip += f"\n跳板机：{jump.display_name if jump else host.jump_host_id}"

                item.setToolTip(tooltip)
                self.host_list.addItem(item)

                if host.id == selected_id:
                    self.host_list.setCurrentItem(item)

        if self.host_list.currentItem() is None:
            self._select_first_host()

        self._update_window_title()

    def _select_first_host(self) -> None:
        """跳过分组标题，选中第一个真实主机。"""

        for row in range(self.host_list.count()):
            item = self.host_list.item(row)

            if item.data(STATE_ROLE):
                self.host_list.setCurrentItem(item)
                return

    def _selected_host(self) -> Host | None:
        item = self.host_list.currentItem()

        if item is None:
            return None

        return self.context.hosts.get(item.data(STATE_ROLE))

    def _show_host_menu(self, position) -> None:
        """主机列表右键菜单（支持多选批量操作）。"""

        hosts = self._selected_hosts()

        if not hosts:
            return

        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        single = len(hosts) == 1

        menu.addAction("连接", self.connect_selected)
        menu.addAction(f"连接选中的 {len(hosts)} 台", self.connect_selected_hosts)
        menu.addSeparator()
        menu.addAction("编辑主机", self.edit_host).setEnabled(single)
        menu.addAction("SFTP 文件", self.open_sftp).setEnabled(single)
        menu.addAction("端口转发", self.open_forwards).setEnabled(single)
        menu.addSeparator()
        menu.addAction("断开选中会话", self.disconnect_selected_hosts)
        menu.addAction(f"删除选中的 {len(hosts)} 台", self.delete_selected_hosts)
        menu.exec(self.host_list.viewport().mapToGlobal(position))

    def _on_host_selected(self, *_args) -> None:
        host = self._selected_host()

        if host is None:
            self.statusBar().showMessage("未选择主机")
            return

        self.statusBar().showMessage(f"{host.display_name} · {host.target} · 认证: {host.auth_type}")

    def new_host(self) -> None:
        dialog = HostDialog(hosts_for_jump=self.context.hosts.sorted_hosts(), parent=self)

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
        dialog = HostDialog(
            host=host,
            known_password=known,
            hosts_for_jump=self.context.hosts.sorted_hosts(),
            parent=self,
        )

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
        hosts = self._selected_hosts()

        if not hosts:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        if len(hosts) > 1:
            self.delete_selected_hosts()
            return

        host = hosts[0]
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

    def _session_for(self, host_id: str) -> SessionTab | None:
        """取该主机当前的终端面板（分屏时取焦点所在的那个）。"""

        widget = self.tabs.get(host_id)

        if isinstance(widget, SplitTerminalTab):
            return widget.active_pane()

        if isinstance(widget, SessionTab):
            return widget

        return None

    def _iter_terminal_tabs(self) -> list:
        """所有终端类标签（普通会话、分屏、本地终端）。"""

        return [*self.tabs.values(), *self.local_tabs.values()]

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

        session = self._session_for(host.id)
        existing = session.session.client if session is not None and session.session is not None else None

        dialog = ForwardDialog(
            host,
            credential=credential,
            jump=self._jump_client_for(host),
            existing_client=existing,
            parent=self,
        )
        dialog.exec()
        self.statusBar().showMessage(f"{host.display_name} 端口转发已关闭")

    # ------------------------------------------------------------------
    # 密钥管理
    # ------------------------------------------------------------------

    def _active_clients(self) -> dict[str, SSHClient]:
        """可复用的活跃连接：跳板机 + 已打开的终端会话。"""

        clients = {host_id: client for host_id, client in self.context.jump_clients.items() if client.is_connected()}

        for host_id, tab in self.tabs.items():
            pane = tab.active_pane() if isinstance(tab, SplitTerminalTab) else tab
            session = getattr(pane, "session", None)
            client = getattr(session, "client", None)

            if client is not None and client.is_connected():
                clients[host_id] = client

        return clients

    def manage_keys(self) -> None:
        dialog = KeyManagerDialog(
            self.context.key_store(),
            self.context.hosts,
            self.context.credentials,
            clients=self._active_clients(),
            parent=self,
        )
        dialog.exec()

    # ------------------------------------------------------------------
    # 会话录制 / 回放
    # ------------------------------------------------------------------

    def toggle_recording(self) -> None:
        pane = self._focused_terminal()

        if pane is None:
            self.statusBar().showMessage("请先打开一个终端标签")
            return

        if pane.recording:
            path = pane.stop_recording()
            self.action_record.setText("开始录制会话")
            self.record_timer.stop()

            if path is not None:
                self.statusBar().showMessage(f"录制已保存：{path}")

            self._update_title()
            return

        from ..recording import default_recordings_dir, suggest_filename

        directory = default_recordings_dir()
        suggested = directory / suggest_filename(pane.display_name)
        path, _ = QFileDialog.getSaveFileName(self, "保存录制", str(suggested), "asciicast 录制 (*.cast)")

        if not path:
            return

        pane.start_recording(Path(path))
        self.action_record.setText("停止录制")
        self.record_timer.start()
        self.statusBar().showMessage(f"正在录制：{path}")

    def _on_record_tick(self) -> None:
        pane = self._focused_terminal()

        if pane is None or not pane.recording:
            self.record_timer.stop()
            return

        recorder = pane.recorder
        self.statusBar().showMessage(
            f"● 录制中 {recorder.elapsed:.0f}s · {recorder.event_count} 事件 · {recorder.byte_count} 字节"
        )

    def open_replay(self) -> None:
        if self.replay_dialog is None:
            self.replay_dialog = ReplayDialog(parent=self)

        self.replay_dialog.show()
        self.replay_dialog.raise_()
        self.replay_dialog.activateWindow()

    # ------------------------------------------------------------------
    # 配置同步 / PPK 导入 / 批量操作
    # ------------------------------------------------------------------

    def sync_config(self) -> None:
        from ..sync import export_package, import_package

        folder = QFileDialog.getExistingDirectory(self, "选择同步目录（共享盘 / 网盘同步目录）")

        if not folder:
            return

        choice = QMessageBox.question(
            self,
            "配置同步",
            "选择同步方向：\n\n是 = 导出到该目录（覆盖目录里的清单）\n否 = 从该目录导入并合并到本地",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
        )

        if choice == QMessageBox.StandardButton.Cancel:
            return

        if choice == QMessageBox.StandardButton.Yes:
            include = QMessageBox.question(
                self,
                "配置同步",
                "是否同时导出已保存的密码？\n（用当前 Windows 账户加密，换机器通常无法解密）",
            ) == QMessageBox.StandardButton.Yes

            try:
                path = export_package(
                    Path(folder),
                    self.context.hosts.sorted_hosts(),
                    credentials=self.context.credentials,
                    include_credentials=include,
                )
            except OSError as error:
                QMessageBox.warning(self, APP_NAME, f"导出失败：{error}")
                return

            QMessageBox.information(self, APP_NAME, f"已导出 {len(self.context.hosts.hosts)} 台主机到：\n{path}")
            self.statusBar().showMessage(f"配置已导出到 {path}")
            return

        result = import_package(Path(folder), self.context.hosts.hosts, self.context.credentials)
        self.context.hosts.save()
        self.refresh_hosts()
        QMessageBox.information(self, APP_NAME, result.summary("导入完成"))
        self.statusBar().showMessage(result.summary("配置同步"))

    def import_ppk(self) -> None:
        from ..keygen import KeyError_ as KeyGenError
        from ..ppk import PPKError, convert_ppk, parse_ppk

        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "导入 PuTTY 密钥",
            str(Path.home()),
            "PuTTY 密钥 (*.ppk);;所有文件 (*)",
        )

        if not paths:
            return

        store = self.context.key_store()
        imported = 0
        errors: list[str] = []

        for path_text in paths:
            path = Path(path_text)

            try:
                meta = parse_ppk(path)
                passphrase = ""

                if meta.encrypted:
                    text, ok = QInputDialog.getText(
                        self,
                        "导入 PuTTY 密钥",
                        f"{path.name} 已加密，请输入 passphrase：",
                        QInputDialog.EchoMode.Password,
                    )

                    if not ok:
                        continue

                    passphrase = text

                private_key, comment = convert_ppk(path, passphrase)

                from ..keygen import ManagedKey, public_key_text

                filename = f"ppk-{path.stem}"
                target = store.directory / filename
                counter = 1

                while target.exists():
                    filename = f"ppk-{path.stem}-{counter}"
                    target = store.directory / filename
                    counter += 1

                private_key.write_private_key_file(str(target))

                key = ManagedKey(
                    name=path.stem,
                    key_type="rsa" if meta.key_type == "ssh-rsa" else "ed25519",
                    comment=comment or path.stem,
                    filename=filename,
                )
                store.add(key)
                key.public_path.write_text(f"{public_key_text(private_key)} {key.comment}\n", encoding="utf-8")
                imported += 1
            except (PPKError, KeyGenError) as error:
                errors.append(f"{path.name}：{error}")
            except Exception as error:  # noqa: BLE001
                errors.append(f"{path.name}：{error}")

        message = f"已导入 {imported} 个密钥到密钥目录。"

        if errors:
            message += "\n\n失败：\n" + "\n".join(errors[:5])

        QMessageBox.information(self, APP_NAME, message)
        self.statusBar().showMessage(f"导入 PuTTY 密钥 {imported} 个")

    def _selected_hosts(self) -> list[Host]:
        """当前选中的主机（支持多选）。"""

        hosts = []

        for item in self.host_list.selectedItems():
            host_id = item.data(STATE_ROLE)
            host = self.context.hosts.get(host_id) if host_id else None

            if host is not None:
                hosts.append(host)

        return hosts

    def connect_selected_hosts(self) -> None:
        """批量连接选中的主机（依次错开 300ms，避免同时握手）。"""

        hosts = self._selected_hosts()

        if not hosts:
            self.statusBar().showMessage("请先在左侧选择主机（可按住 Ctrl 多选）")
            return

        for index, host in enumerate(hosts):
            QTimer.singleShot(index * 300, lambda h=host: self.connect_host(h))

        self.statusBar().showMessage(f"正在连接 {len(hosts)} 台主机…")

    def disconnect_selected_hosts(self) -> None:
        hosts = self._selected_hosts()
        closed = 0

        for host in hosts:
            tab = self.tabs.get(host.id)

            if tab is None:
                continue

            index = self.tabs_widget.indexOf(tab)

            if index >= 0:
                self.close_tab(index)
                closed += 1

        self.statusBar().showMessage(f"已断开 {closed} 台主机的会话")

    def delete_selected_hosts(self) -> None:
        hosts = self._selected_hosts()

        if not hosts:
            return

        names = "、".join(host.display_name for host in hosts[:5])
        confirm = QMessageBox.question(self, APP_NAME, f"删除 {len(hosts)} 台主机（{names}）及其保存的密码？")

        if confirm != QMessageBox.StandardButton.Yes:
            return

        for host in hosts:
            self.context.credentials.delete(host.id)
            self.context.hosts.remove(host.id)

        self.refresh_hosts()
        self.statusBar().showMessage(f"已删除 {len(hosts)} 台主机")

    # ------------------------------------------------------------------
    # 本地终端 / 分屏
    # ------------------------------------------------------------------

    def open_local_terminal(self) -> None:
        if not conpty_available():
            QMessageBox.warning(
                self,
                APP_NAME,
                "本地终端需要 pywinpty 才能使用（ConPTY）。\n请在项目虚拟环境执行：pip install pywinpty",
            )
            return

        tab = LocalTerminalTab(parent=self)
        index = self.tabs_widget.addTab(tab, "本地终端")
        self.tabs_widget.setTabToolTip(index, tab.host.hostname)
        self.tabs_widget.setCurrentIndex(index)

        self.local_tabs[tab.host.id] = tab
        self._update_window_title()

    def split_current(self, orientation: Qt.Orientation) -> None:
        """把当前终端标签拆成两个面板（各自独立连接）。"""

        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, LocalTerminalTab):
            self.statusBar().showMessage("本地终端暂不支持分屏")
            return

        if isinstance(widget, SplitTerminalTab):
            pane = widget.split(orientation)

            if pane is None:
                self.statusBar().showMessage(f"分屏数量已达上限（{SplitTerminalTab.MAX_PANES}）")
            else:
                pane.view.setFocus()

            return

        if not isinstance(widget, SessionTab):
            self.statusBar().showMessage("请先打开一个终端标签再分屏")
            return

        host = widget.host
        index = self.tabs_widget.indexOf(widget)
        title = self.tabs_widget.tabText(index)

        # 复用原有面板，避免白建一条连接；先隐藏再换父控件，防止 Qt 布局递归
        widget.setVisible(False)
        split = SplitTerminalTab(host, credential=widget.credential, jump=widget.jump, orientation=orientation, parent=self)
        split.splitter.insertWidget(0, widget)
        widget.setVisible(True)
        self.tabs_widget.removeTab(index)

        split.title_changed.connect(lambda text, h=host: self._on_title(h, text))
        split.failed.connect(lambda message, h=host: self._on_session_error(h, message))

        new_index = self.tabs_widget.addTab(split, title or host.display_name)
        self.tabs_widget.setTabToolTip(new_index, host.target)
        self.tabs_widget.setCurrentIndex(new_index)

        self.tabs[host.id] = split
        split.add_pane(activate=True)
        self._update_window_title()

    def focus_next_pane(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SplitTerminalTab):
            widget.focus_next()
        else:
            self.statusBar().showMessage("当前标签没有分屏")

    # ------------------------------------------------------------------
    # 主机导入 / 导出
    # ------------------------------------------------------------------

    def import_hosts(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入主机",
            str(Path.home()),
            "主机文件 (*.txt *.conf *.config *.json);;所有文件 (*)",
        )

        if not path:
            return

        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            QMessageBox.warning(self, APP_NAME, f"读取文件失败：{error}")
            return

        if path.lower().endswith(".json"):
            result = import_json(text)
            secrets = import_json_credentials(text)
        else:
            result = parse_openssh_config(text)
            secrets = {}

        if not result.hosts and not result.warnings:
            QMessageBox.information(self, APP_NAME, "没有解析到任何主机。")
            return

        existing = {(host.hostname, host.port, host.username) for host in self.context.hosts.hosts}
        added = 0
        imported: list[Host] = []

        for host in result.hosts:
            if (host.hostname, host.port, host.username) in existing:
                continue

            self.context.hosts.add(host)
            existing.add((host.hostname, host.port, host.username))
            imported.append(host)
            added += 1

            secret = secrets.get(host.id)

            if secret:
                self.context.credentials.set(host.id, secret)

        # ProxyJump：把别名解析成真实主机 id（跳板机可能就在同一份配置里）
        jumps = resolve_proxy_jumps(result, imported) if imported else 0

        if jumps:
            self.context.hosts.save()

        self.refresh_hosts()

        message = f"导入完成：新增 {added} 台主机（跳过重复 {result.count - added} 台）。"

        if jumps:
            message += f"\n已解析 {jumps} 条 ProxyJump 跳板机关系。"

        if result.skipped:
            message += f"\n忽略 {len(result.skipped)} 个通配/空条目。"

        if result.warnings:
            message += "\n提示：\n" + "\n".join(result.warnings[:5])

        QMessageBox.information(self, APP_NAME, message)
        self.statusBar().showMessage(f"已从 {Path(path).name} 导入 {added} 台主机")

    def export_hosts(self) -> None:
        if not self.context.hosts.hosts:
            QMessageBox.information(self, APP_NAME, "当前没有可导出的主机。")
            return

        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出主机",
            str(Path.home() / "pyssh-hosts.txt"),
            "SSH config (*.txt *.conf);;JSON (*.json)",
        )

        if not path:
            return

        as_json = path.lower().endswith(".json") or "JSON" in selected_filter

        include_credentials = False

        if as_json:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "是否把已保存的密码一起导出？\n（密码用当前 Windows 账户加密，换机器通常无法解密）",
            )

            include_credentials = answer == QMessageBox.StandardButton.Yes

        try:
            if as_json:
                text = export_json(
                    self.context.hosts.sorted_hosts(),
                    credentials=self.context.credentials,
                    include_credentials=include_credentials,
                )
            else:
                text = export_openssh_config(self.context.hosts.sorted_hosts())

            Path(path).write_text(text, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, APP_NAME, f"写入失败：{error}")
            return

        self.statusBar().showMessage(f"已导出 {len(self.context.hosts.hosts)} 台主机到 {path}")

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

        if isinstance(tab, SplitTerminalTab):
            tab.reconnect_all()
        elif tab is not None:
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

        if isinstance(widget, (SessionTab, SplitTerminalTab, SftpPanel, LocalTerminalTab)):
            self._select_host(widget.host.id)

            if isinstance(widget, SessionTab):
                widget.view.setFocus()
            elif isinstance(widget, SplitTerminalTab) and widget.active_pane() is not None:
                widget.active_pane().view.setFocus()

        self._update_window_title()

    # ------------------------------------------------------------------
    # 标签管理
    # ------------------------------------------------------------------

    def close_current_tab(self) -> None:
        self.close_tab(self.tabs_widget.currentIndex())

    def close_tab(self, index: int) -> None:
        widget = self.tabs_widget.widget(index)

        if isinstance(widget, SftpPanel):
            host = widget.host
            widget.close()
            self.tabs_widget.removeTab(index)

            if self.sftp_tabs.get(host.id) is widget:
                del self.sftp_tabs[host.id]
        elif isinstance(widget, LocalTerminalTab):
            host = widget.host
            widget.close()
            self.tabs_widget.removeTab(index)

            if self.local_tabs.get(host.id) is widget:
                del self.local_tabs[host.id]
        elif isinstance(widget, (SessionTab, SplitTerminalTab)):
            host = widget.host
            widget.close()
            self.tabs_widget.removeTab(index)

            if self.tabs.get(host.id) is widget:
                del self.tabs[host.id]
        else:
            return

        self._update_window_title()

    def reconnect_current(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SftpPanel):
            self.statusBar().showMessage("SFTP 标签不支持重连，请关闭后重新打开")
            return

        if isinstance(widget, LocalTerminalTab):
            self.statusBar().showMessage("本地终端不支持重连，请重新打开一个")
            return

        if isinstance(widget, SplitTerminalTab):
            widget.credential = self._ensure_credential(widget.host)
            widget.jump = self._jump_client_for(widget.host)
            widget.reconnect_all()
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

    def _focused_terminal(self):
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            return widget

        if isinstance(widget, SplitTerminalTab):
            return widget.active_pane()

        return None

    def copy_from_terminal(self) -> None:
        pane = self._focused_terminal()

        if pane is not None:
            pane.view.copy()

    def paste_to_terminal(self) -> None:
        pane = self._focused_terminal()

        if pane is not None:
            pane.view.paste()

    # ------------------------------------------------------------------

    def _update_window_title(self) -> None:
        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            self.setWindowTitle(f"{APP_NAME} {__version__} — {widget.display_name}")
        elif isinstance(widget, SplitTerminalTab):
            self.setWindowTitle(f"{APP_NAME} {__version__} — {widget.display_name}（分屏）")
        elif isinstance(widget, SftpPanel):
            self.setWindowTitle(f"{APP_NAME} {__version__} — SFTP {widget.host.display_name}")
        else:
            self.setWindowTitle(f"{APP_NAME} {__version__}")

    def closeEvent(self, event):  # noqa: N802
        self._quitting = True

        for tab in list(self.tabs.values()):
            tab.close()

        for tab in list(self.local_tabs.values()):
            tab.close()

        for panel in list(self.sftp_tabs.values()):
            panel.close()

        self.tabs.clear()
        self.local_tabs.clear()
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
