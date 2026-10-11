"""新功能 GUI 验证：本地终端、分屏、三种端口转发对话框、主机导入导出。

会短暂弹窗并截图到 build/。

运行::

    .venv\\Scripts\\python.exe tests\\gui_features_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# 测试必须与真实数据目录隔离，避免污染主机列表与凭据
os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-features-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer

from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.forwarding import KIND_DYNAMIC, KIND_LOCAL, KIND_REMOTE, ForwardRule
from sshmanager.hostio import export_json, export_openssh_config, import_json, parse_openssh_config
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, ForwardDialog, MainWindow, SplitTerminalTab

build_dir = Path(__file__).resolve().parent.parent / "build"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--local-port", type=int, default=18111)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = next(
        (item for item in context.hosts.hosts if item.hostname == args.host and item.port == args.port),
        None,
    )

    if host is None:
        host = Host(label=args.host, hostname=args.host, port=args.port, username=args.username)
        context.hosts.add(host)

    if args.password:
        context.credentials.set(args.host and host.id, args.password)

    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()

    build_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "local_terminal": False,
        "ssh_session": False,
        "split": False,
        "forward_dialog": False,
        "hostio": False,
    }

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    deadline = time.time() + args.timeout

    # ------------------------------------------------------------------
    # 1) 本地终端（ConPTY）
    # ------------------------------------------------------------------
    def step_local() -> None:
        window.open_local_terminal()
        tab = next(iter(window.local_tabs.values()), None)

        if tab is None:
            print("本地终端标签未创建", flush=True)
            app.quit()
            return

        def poll() -> None:
            text = tab.view.emulator.plain_text()

            if text.strip():
                state["local_terminal"] = True
                print(f"1. 本地终端输出（前 60 字）：{text.strip()[:60]!r}", flush=True)
                tab.view.apply_theme({})  # 触发一次重绘
                window.grab().save(str(build_dir / "local-terminal.png"))
                QTimer.singleShot(300, step_ssh)
                return

            if time.time() > deadline:
                print("本地终端没有输出", flush=True)
                app.quit()
                return

            QTimer.singleShot(300, poll)

        QTimer.singleShot(600, poll)

    # ------------------------------------------------------------------
    # 2) SSH 会话 + 分屏
    # ------------------------------------------------------------------
    def step_ssh() -> None:
        window.connect_host(host)

        def poll() -> None:
            pane = window._session_for(host.id)

            if pane is not None and any(marker in pane.view.emulator.plain_text() for marker in ("#", "$", ">")):
                state["ssh_session"] = True
                print("2. SSH 会话已就绪", flush=True)
                QTimer.singleShot(200, step_split)
                return

            if time.time() > deadline:
                print("SSH 会话未就绪", flush=True)
                app.quit()
                return

            QTimer.singleShot(400, poll)

        QTimer.singleShot(300, poll)

    def step_split() -> None:
        window.split_current(Qt.Orientation.Horizontal)
        # window.tabs 现在是「主机 id → 标签列表」，分屏后该主机的标签变成 SplitTerminalTab
        split = next(
            (tab for tab in window._tabs_for_host(host.id) if isinstance(tab, SplitTerminalTab)),
            None,
        )

        if split is None:
            print("分屏标签未创建", flush=True)
            app.quit()
            return

        def poll() -> None:
            panes = split.panes

            if len(panes) >= 2 and all(
                any(marker in pane.view.emulator.plain_text() for marker in ("#", "$", ">")) for pane in panes
            ):
                state["split"] = True
                print(f"3. 分屏面板数：{len(panes)}，均已有远端输出", flush=True)
                # 给两个面板各发一条命令，确认是两条独立会话
                panes[0].session.send(b"echo PANE-A\r\n")
                panes[1].session.send(b"echo PANE-B\r\n")
                QTimer.singleShot(1200, capture_and_next)
                return

            if time.time() > deadline:
                print(f"分屏未就绪（面板数 {len(panes)}）", flush=True)
                app.quit()
                return

            QTimer.singleShot(500, poll)

        QTimer.singleShot(400, poll)

    def capture_and_next() -> None:
        split = next(
            (tab for tab in window._tabs_for_host(host.id) if isinstance(tab, SplitTerminalTab)),
            None,
        )
        panes = split.panes if isinstance(split, SplitTerminalTab) else []

        a_ok = any("PANE-A" in pane.view.emulator.plain_text() for pane in panes)
        b_ok = any("PANE-B" in pane.view.emulator.plain_text() for pane in panes)
        print(f"   两条独立会话各自回显：PANE-A={a_ok} PANE-B={b_ok}", flush=True)

        window.grab().save(str(build_dir / "split-view.png"))
        QTimer.singleShot(200, step_forwards)

    # ------------------------------------------------------------------
    # 3) 端口转发对话框（三种类型）
    # ------------------------------------------------------------------
    def step_forwards() -> None:
        session = window._session_for(host.id)
        existing = session.session.client if session is not None and session.session is not None else None

        dialog = ForwardDialog(host, credential=args.password, existing_client=existing, parent=window)
        dialog.resize(860, 480)
        dialog.show()

        dialog.rules = [
            ForwardRule(kind=KIND_LOCAL, bind="127.0.0.1", local_port=args.local_port, remote_host="127.0.0.1", remote_port=22, label="本地-L"),
            ForwardRule(kind=KIND_REMOTE, bind="127.0.0.1", remote_port=args.local_port + 1, remote_host="127.0.0.1", local_port=18991, label="远端-R"),
            ForwardRule(kind=KIND_DYNAMIC, bind="127.0.0.1", local_port=args.local_port + 2, label="动态-D"),
        ]
        dialog._persist()
        dialog.refresh()
        app.processEvents()

        # 开启其中一条，确认状态列会变成运行中
        dialog.table.selectRow(2)
        app.processEvents()
        dialog.start_selected()
        app.processEvents()

        rows = [
            [dialog.table.item(r, c).text() if dialog.table.item(r, c) else "" for c in range(5)]
            for r in range(dialog.table.rowCount())
        ]

        for row in rows:
            print(f"   规则：{row}", flush=True)

        state["forward_dialog"] = len(rows) == 3 and any("运行中" in row[4] for row in rows)
        print(f"4. 转发对话框：{len(rows)} 条规则，活动 {dialog.manager.active_count()} 条", flush=True)

        dialog.grab().save(str(build_dir / "forward-dialog-3kinds.png"))
        dialog.manager.stop_all()
        dialog.close()

        QTimer.singleShot(200, step_hostio)

    # ------------------------------------------------------------------
    # 4) 主机导入导出
    # ------------------------------------------------------------------
    def step_hostio() -> None:
        with tempfile.TemporaryDirectory(prefix="pyssh-io-") as workspace:
            workspace_path = Path(workspace)
            sample_hosts = [
                Host(label="web", hostname="10.0.0.10", port=2222, username="deploy"),
                Host(label="db", hostname="10.0.0.20", username="root", verify_host_key=False),
            ]

            config_text = export_openssh_config(sample_hosts)
            (workspace_path / "config").write_text(config_text, encoding="utf-8")

            parsed = parse_openssh_config(config_text)
            json_text = export_json(sample_hosts)
            (workspace_path / "hosts.json").write_text(json_text, encoding="utf-8")
            parsed_json = import_json(json_text)

            config_ok = parsed.count == 2 and {h.label for h in parsed.hosts} == {"web", "db"}
            json_ok = parsed_json.count == 2
            state["hostio"] = config_ok and json_ok

            print(f"5. 导出/导入：ssh config {parsed.count} 台，JSON {parsed_json.count} 台", flush=True)
            print(f"   导出的 config 片段：{config_text.splitlines()[:4]}", flush=True)

        finish()

    def finish() -> None:
        watchdog.stop()
        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step_local)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("GUI_FEATURES_E2E_OK" if ok else "GUI_FEATURES_E2E_FAILED", flush=True)

    # 跳过解释器退出阶段（PySide6 6.11 + Python 3.14 退出期可能因 C 层残留线程崩溃）
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
