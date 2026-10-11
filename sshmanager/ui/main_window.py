"""主窗口：左侧主机列表 + 右侧多标签终端。"""

from __future__ import annotations

from dataclasses import dataclass, field

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLineEdit,
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
        #: 说明：终端标签统一从 tabs_widget 现算（见 _all_host_tabs / _tabs_for_host），
        #: 不再维护单独的登记表——曾经两者不同步导致退出时漏关会话。
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

        self.action_new_session = QAction("新会话", self)
        self.action_new_session.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.action_new_session.triggered.connect(self.new_session_for_selected)

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

        self.action_command_library = QAction("命令库（收藏 / 历史）", self)
        self.action_command_library.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.action_command_library.triggered.connect(self.open_command_library)

        self.action_copy_output = QAction("复制全部输出到 Windows", self)
        self.action_copy_output.setShortcut(QKeySequence("Ctrl+Alt+C"))
        self.action_copy_output.triggered.connect(self.copy_all_output)

        self.action_save_output = QAction("保存输出到文件…", self)
        self.action_save_output.setShortcut(QKeySequence("Ctrl+Alt+S"))
        self.action_save_output.triggered.connect(self.save_all_output)

        self.action_favorite_selection = QAction("把选中内容收藏为命令", self)
        self.action_favorite_selection.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.action_favorite_selection.setToolTip("把终端里选中的文字存进命令库")
        self.action_favorite_selection.triggered.connect(self.favorite_selection)

        self.action_select_to_copy = QAction("选中即复制（Linux→Windows）", self)
        self.action_select_to_copy.setToolTip("鼠标选中终端文字后自动复制到 Windows 剪贴板")
        # 先设好初始状态再连信号：菜单是在创建 tabs_widget 之前构建的，
        # 若先连接，setChecked(True) 会立刻触发槽函数去访问还不存在的控件。
        self.action_select_to_copy.setCheckable(True)
        self.action_select_to_copy.setChecked(True)
        self.action_select_to_copy.toggled.connect(self.set_select_to_copy)

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
        toolbar.addAction(self.action_new_session)
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
        menu.addAction(self.action_new_session)
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
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_command_library)
        edit_menu.addAction(self.action_favorite_selection)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_copy_output)
        edit_menu.addAction(self.action_save_output)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_select_to_copy)

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
        self._sync_command_actions()

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
                # 已开会话时在条目上标出数量和序号
                open_count = len(self._tabs_for_host(host.id))
                badge = f"  ×{open_count}" if open_count else ""
                item = QListWidgetItem(f"{host.display_name}{badge}\n{host.target}")
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
        menu.addAction("新会话（同一主机多条）", self.new_session_for_selected).setEnabled(single)
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

    def new_session_for_selected(self) -> None:
        """为选中主机再开一个会话（同一主机可以有多条并行会话）。"""

        host = self._selected_host()

        if host is None:
            QMessageBox.information(self, APP_NAME, "请先在左侧选择一台主机。")
            return

        self.connect_host(host, force_new=True)

    def connect_host(self, host: Host, force_new: bool = False) -> None:
        """连接主机。

        默认复用已有的第一个会话（把标签切到前台）；``force_new=True``
        时无论是否已有会话都再开一条新会话。
        """

        if not force_new:
            existing = self._first_tab_for_host(host.id)

            if existing is not None:
                self.tabs_widget.setCurrentWidget(existing)
                self.statusBar().showMessage(f"{host.display_name} 已有会话，已切到该标签（要再开一条请用「新会话」）")
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

        self._refresh_tab_title(host)
        self.context.hosts.mark_connected(host.id)
        self.refresh_hosts()
        self._update_window_title()
        self._sync_command_actions()

    # ------------------------------------------------------------------
    # 同一主机多会话的辅助
    # ------------------------------------------------------------------

    def _tabs_for_host(self, host_id: str) -> list:
        """该主机当前打开的所有终端标签（普通会话 / 分屏）。"""

        return [
            self.tabs_widget.widget(row)
            for row in range(self.tabs_widget.count())
            if getattr(self.tabs_widget.widget(row), "host", None) is not None
            and self.tabs_widget.widget(row).host.id == host_id
            and isinstance(self.tabs_widget.widget(row), (SessionTab, SplitTerminalTab))
        ]

    def _first_tab_for_host(self, host_id: str):
        tabs = self._tabs_for_host(host_id)

        return tabs[0] if tabs else None

    def _session_count(self, host: Host) -> int:
        """该主机的终端面板总数（分屏按面板数计）。"""

        count = 0

        for widget in self._tabs_for_host(host.id):
            if isinstance(widget, SplitTerminalTab):
                count += max(1, len(widget.panes))
            else:
                count += 1

        return count

    def _pane_ordinal(self, pane) -> int:
        """该面板在同主机所有面板里的序号（从 1 开始）。"""

        host = getattr(pane, "host", None)

        if host is None:
            return 0

        ordinal = 0

        for widget in self._tabs_for_host(host.id):
            if isinstance(widget, SplitTerminalTab):
                for candidate in widget.panes:
                    ordinal += 1

                    if candidate is pane:
                        return ordinal
            else:
                ordinal += 1

                if widget is pane:
                    return ordinal

        return ordinal

    def _refresh_tab_title(self, host: Host) -> None:
        """按会话数量更新标签标题：1 条不加后缀，多条显示 (2)(3)…"""

        tabs = self._tabs_for_host(host.id)

        if not tabs:
            return

        ordinal = 0

        for widget in tabs:
            index = self.tabs_widget.indexOf(widget)

            if index < 0:
                continue

            if isinstance(widget, SplitTerminalTab):
                for pane in widget.panes:
                    ordinal += 1
                    title = self._titled(widget, pane, ordinal, len(tabs))
                    # 分屏容器只有一个标签，标题用整个容器的
                    self.tabs_widget.setTabText(index, title)
                    break

                ordinal += max(0, len(widget.panes) - 1)
            else:
                ordinal += 1
                self.tabs_widget.setTabText(index, self._titled(widget, widget, ordinal, len(tabs)))

    @staticmethod
    def _titled(widget, pane, ordinal: int, tab_count: int) -> str:
        name = getattr(widget, "display_name", "") or "会话"

        if tab_count <= 1:
            return name[:40] if widget is pane else f"{name} ({ordinal})"

        return f"{name} ({ordinal})"[:40]

    def _session_for(self, host_id: str) -> SessionTab | None:
        """取该主机当前的终端面板（优先焦点标签，兼容分屏）。"""

        current = self.tabs_widget.currentWidget()

        if isinstance(current, SplitTerminalTab) and current.host.id == host_id:
            return current.active_pane()

        if isinstance(current, SessionTab) and current.host.id == host_id:
            return current

        first = self._first_tab_for_host(host_id)

        if isinstance(first, SplitTerminalTab):
            return first.active_pane()

        return first if isinstance(first, SessionTab) else None

    def _iter_terminal_tabs(self) -> list:
        """所有终端类标签（普通会话、分屏、本地终端）。"""

        return [*self._all_host_tabs(), *self.local_tabs.values()]

    def _all_host_tabs(self) -> list:
        """所有主机终端标签（同一主机可能有多条）。

        直接从标签控件现算，避免与登记表不同步——曾经用 self.tabs 做登记表，
        普通连接路径把标签追加到了临时列表上，导致退出时漏关会话。
        """

        tabs_widget = getattr(self, "tabs_widget", None)

        if tabs_widget is None:  # 界面还在构建中（菜单动作可能在控件创建前触发）
            return []

        return [
            tabs_widget.widget(row)
            for row in range(tabs_widget.count())
            if isinstance(tabs_widget.widget(row), (SessionTab, SplitTerminalTab))
        ]

    def _ensure_credential(self, host: Host) -> str | None:
        """取主机凭据；密码认证且没有保存过时，向用户询问一次并加密保存。"""

        credential = self.context.credentials.get(host.id)

        if host.auth_type != "password" or credential:
            return credential

        text, ok = QInputDialog.getText(
            self,
            APP_NAME,
            f"请输入 {host.target} 的密码（将加密保存到本机）：",
            QLineEdit.EchoMode.Normal,
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
        panel.progress_text.connect(lambda text, h=host: self._on_transfer_progress(h, text))
        index = self.tabs_widget.addTab(panel, f"SFTP · {host.display_name}")
        self.tabs_widget.setTabToolTip(index, host.target)
        self.tabs_widget.setCurrentIndex(index)

        self.sftp_tabs[host.id] = panel
        self._update_window_title()

    def _on_transfer_progress(self, host: Host, text: str) -> None:
        """把 SFTP 传输百分比同时显示在标签标题与状态栏上。"""

        panel = self.sftp_tabs.get(host.id)

        if panel is None:
            return

        index = self.tabs_widget.indexOf(panel)

        if index < 0:
            return

        if text:
            self.tabs_widget.setTabText(index, f"{text[:60]} · {host.display_name}")
            self.statusBar().showMessage(text)
        else:
            self.tabs_widget.setTabText(index, f"SFTP · {host.display_name}")
            self.statusBar().showMessage("就绪")

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

        for tab in self._all_host_tabs():
            host = getattr(tab, "host", None)

            if host is None:
                continue

            pane = tab.active_pane() if isinstance(tab, SplitTerminalTab) else tab
            session = getattr(pane, "session", None)
            client = getattr(session, "client", None)

            if client is not None and client.is_connected():
                clients.setdefault(host.id, client)

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
                        QLineEdit.EchoMode.Password,
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

        # 关闭选中主机的全部会话（含分屏与多开）
        for host in hosts:
            for tab in list(self._tabs_for_host(host.id)):
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

        split.add_pane(activate=True)
        self._refresh_tab_title(host)
        self._update_window_title()
        self._sync_command_actions()

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

        tab = self._first_tab_for_host(host.id)

        if isinstance(tab, SplitTerminalTab):
            tab.reconnect_all()
        elif tab is not None:
            tab.reconnect()

    def _on_title(self, host: Host, title: str) -> None:
        """远端 OSC 改了标题：把它作为该主机所有标签的标题基础。"""

        if not title:
            return

        for tab in self._tabs_for_host(host.id):
            tab.remote_title = title[:40]

        self._refresh_tab_title_by_title(host)
        self._update_window_title()

    def _refresh_tab_title_by_title(self, host: Host) -> None:
        """按"远端标题优先"的规则刷新标签文字，并保留多会话序号。"""

        tabs = self._tabs_for_host(host.id)

        if not tabs:
            return

        base = getattr(tabs[0], "remote_title", "") or (host.display_name)
        ordinal = 0

        for widget in tabs:
            index = self.tabs_widget.indexOf(widget)

            if index < 0:
                continue

            if isinstance(widget, SplitTerminalTab):
                ordinal += 1
                text = base if len(tabs) <= 1 and ordinal == 1 else f"{base} ({ordinal})"
                self.tabs_widget.setTabText(index, text[:40])
                ordinal += max(0, len(widget.panes) - 1)
            else:
                ordinal += 1
                text = base if len(tabs) <= 1 else f"{base} ({ordinal})"
                self.tabs_widget.setTabText(index, text[:40])

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
        self._sync_command_actions()

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

            # removeTab 之后该控件已不在 tabs_widget 里，剩下的标签标签名重新编号
            if self._tabs_for_host(host.id):
                self._refresh_tab_title(host)
        else:
            return

        self._update_window_title()
        self.refresh_hosts()
        self._sync_command_actions()

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

    # ------------------------------------------------------------------
    # 命令：输入栏 / 命令库 / 输出导出
    # ------------------------------------------------------------------

    def _all_terminal_panes(self) -> list:
        """当前标签里的所有终端面板（分屏时返回所有面板）。"""

        widget = self.tabs_widget.currentWidget()

        if isinstance(widget, SessionTab):
            return [widget]

        if isinstance(widget, SplitTerminalTab):
            return list(widget.panes)

        return []

    def _sync_command_actions(self) -> None:
        """命令相关菜单项只在当前标签是终端时可用。"""

        has_terminal = bool(self._all_terminal_panes())

        for action in (
            self.action_command_library,
            self.action_favorite_selection,
            self.action_copy_output,
            self.action_save_output,
        ):
            action.setEnabled(has_terminal)

    def favorite_selection(self) -> None:
        """把终端选中内容收藏成一条命令。"""

        from ..commands import get_store

        pane = self._focused_terminal()

        if pane is None:
            self.statusBar().showMessage("当前没有活动会话")
            return

        text = pane.view.selected_text().strip()

        if not text:
            self.statusBar().showMessage("先在终端里选中要收藏的命令")
            return

        snippet = get_store().add(text, host_id=pane.host.id if pane.host is not None else "")

        if snippet is None:
            self.statusBar().showMessage("这条命令已经在收藏里了")
            return

        self.statusBar().showMessage("已收藏到命令库（Ctrl+Shift+L 打开）")

    def open_command_library(self) -> None:
        from ..commands import get_store
        from .command_library import CommandLibraryDialog

        pane = self._focused_terminal()
        host_id = pane.host.id if pane is not None and pane.host is not None else ""
        label = pane.display_name if pane is not None else "全局"

        dialog = CommandLibraryDialog(get_store(), host_id=host_id, host_label=label, parent=self)

        if pane is not None:
            dialog.send_requested.connect(pane.send_command)

        dialog.exec()

    def copy_all_output(self) -> None:
        pane = self._focused_terminal()

        if pane is None:
            self.statusBar().showMessage("当前没有活动会话")
            return

        text = pane.view.copy_all_output()
        self.statusBar().showMessage(
            f"已复制 {len(text.splitlines())} 行输出到 Windows 剪贴板" if text else "终端还没有输出"
        )

    def save_all_output(self) -> None:
        pane = self._focused_terminal()

        if pane is None:
            self.statusBar().showMessage("当前没有活动会话")
            return

        size = pane.save_output()

        if size:
            self.statusBar().showMessage(f"输出已保存（{size} 字节）")

    def set_select_to_copy(self, enabled: bool) -> None:
        for pane in self._all_terminal_panes_all():
            view = getattr(pane, "view", None)

            if view is not None:
                view.select_to_copy = bool(enabled)

        self.statusBar().showMessage(f"选中即复制：{'开启' if enabled else '关闭'}")

    def _all_terminal_panes_all(self) -> list:
        """所有标签（含所有分屏面板与本地终端）里的终端面板。"""

        panes = []

        for tab in self._iter_terminal_tabs():
            if isinstance(tab, SplitTerminalTab):
                panes.extend(tab.panes)
            else:
                panes.append(tab)

        return panes

    def _paste_clipboard_to_remote(self) -> bool:
        """把 Windows 剪贴板内容发给远端（Windows→Linux）。"""

        from PySide6.QtGui import QGuiApplication

        pane = self._focused_terminal()

        if pane is None:
            self.statusBar().showMessage("当前没有活动会话")
            return False

        text = QGuiApplication.clipboard().text()

        if not text:
            self.statusBar().showMessage("Windows 剪贴板是空的")
            return False

        pane.send_raw(text)
        self.statusBar().showMessage(f"已把剪贴板 {len(text)} 个字符发送到 Linux")

        return True

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

    def shutdown(self) -> None:
        """确定性地停掉所有会话、面板与连接。

        不依赖事件循环：``closeEvent`` 只有在事件循环还在跑时才会被处理，
        测试或 ``app.quit()`` 之后再 ``window.close()`` 就可能整个跳过收尾，
        导致 paramiko 传输层线程残留到进程退出阶段（access violation）。
        """

        self._quitting = True

        # 先让所有会话线程停下来（各自会关掉自己的 SSHClient）
        for tab in self._all_host_tabs():
            try:
                tab.close()
            except Exception:
                pass

        for tab in list(self.local_tabs.values()):
            try:
                tab.close()
            except Exception:
                pass

        for panel in list(self.sftp_tabs.values()):
            try:
                panel.close()
            except Exception:
                pass

        # 兜底：再扫一遍所有 client，确保 socket/传输层都被关闭并 join
        for tab in self._all_host_tabs():
            panes = tab.panes if hasattr(tab, "panes") else [tab]

            for pane in panes:
                session = getattr(pane, "session", None)
                client = getattr(session, "client", None)

                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        for panel in list(self.sftp_tabs.values()):
            worker = getattr(panel, "worker", None)

            if worker is not None:
                client = getattr(worker, "client", None)

                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass

        self.context.close()
        self.local_tabs.clear()
        self.sftp_tabs.clear()

    def closeEvent(self, event):  # noqa: N802
        self.shutdown()
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
