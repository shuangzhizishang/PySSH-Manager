"""SFTP 面板与端口转发对话框的 GUI 验证（会短暂弹窗，并截图到 build/）。

运行::

    .venv\\Scripts\\python.exe tests\\sftp_gui_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, ForwardDialog, MainWindow

MARKER = "pyssh-sftp-gui"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--hold", type=float, default=0.0)
    parser.add_argument("--local-port", type=int, default=18091)
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

    credential = args.password or context.credentials.get(host.id)

    if credential:
        context.credentials.set(host.id, credential)

    print(f"platform={app.platformName()} fonts={len(QFontDatabase.families())}", flush=True)

    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()

    build = Path(__file__).resolve().parent.parent / "build"
    build.mkdir(exist_ok=True)

    state = {"sftp_ready": False, "dialog_ready": False, "listing_ok": False, "forward_ok": False}

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    def report(label: str, image) -> None:
        counter = Counter()

        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                counter[image.pixelColor(x, y).name()] += 1

        print(f"{label}: {counter.most_common(5)}", flush=True)

    # ------------------------------------------------------------------
    # 1) SFTP 面板
    # ------------------------------------------------------------------
    def open_sftp() -> None:
        window.open_sftp()
        panel = window.sftp_tabs.get(host.id)

        if panel is None:
            print("SFTP 面板未创建", flush=True)
            app.quit()
            return

        panel.worker.listing.connect(on_listing)
        panel.worker.failed.connect(lambda message: (print("SFTP_FAILED:", message, flush=True), app.quit()))

    def on_listing(path: str, entries) -> None:
        if state["listing_ok"]:
            return

        state["listing_ok"] = True
        print(f"SFTP 列目录 {path} 共 {len(entries)} 项", flush=True)
        QTimer.singleShot(600, capture_sftp)

    def capture_sftp() -> None:
        panel = window.sftp_tabs.get(host.id)

        window.activateWindow()
        window.raise_()
        app.processEvents()

        image = window.grab().toImage()
        image.save(str(build / "sftp-panel.png"))
        report("SFTP 面板截图", image)

        rows = panel.remote_table.rowCount()
        print(f"SFTP 远端表格行数：{rows}", flush=True)
        state["sftp_ready"] = rows > 0
        QTimer.singleShot(200, open_forward_dialog)

    # ------------------------------------------------------------------
    # 2) 端口转发对话框
    # ------------------------------------------------------------------
    def open_forward_dialog() -> None:
        session = window._session_for(host.id)
        existing = session.session.client if session is not None and session.session is not None else None

        dialog = ForwardDialog(
            host,
            credential=credential,
            existing_client=existing,
            parent=window,
        )
        dialog.resize(760, 460)
        dialog.show()
        app.processEvents()

        # 直接用 API 增加一条规则（跳过模态子对话框），再开启转发
        QTimer.singleShot(300, lambda: add_and_start(dialog))

    def add_and_start(dialog: ForwardDialog) -> None:
        from sshmanager.forwarding import ForwardRule

        dialog.rules.append(
            ForwardRule(bind="127.0.0.1", local_port=args.local_port, remote_host="127.0.0.1", remote_port=22, label="GUI 测试")
        )
        dialog._persist()
        dialog.refresh()

        dialog.table.selectRow(0)
        app.processEvents()
        dialog.start_selected()
        app.processEvents()

        state["forward_ok"] = dialog.manager.active_count() > 0
        print(f"端口转发对话框：活动规则 {dialog.manager.active_count()} 条", flush=True)

        QTimer.singleShot(400, lambda: capture_dialog(dialog, window))

    def capture_dialog(dialog: ForwardDialog, window: MainWindow) -> None:
        dialog.activateWindow()
        dialog.raise_()
        app.processEvents()

        image = dialog.grab().toImage()
        image.save(str(build / "forward-dialog.png"))
        report("端口转发对话框截图", image)

        rows = dialog.table.rowCount()

        for row in range(rows):
            cells = [dialog.table.item(row, column).text() if dialog.table.item(row, column) else "" for column in range(4)]
            print(f"  规则 {row}: {cells}", flush=True)

        state["dialog_ready"] = rows > 0
        dialog.manager.stop_all()
        dialog.close()

        QTimer.singleShot(200, finish)

    def finish() -> None:
        watchdog.stop()

        panel = window.sftp_tabs.get(host.id)

        if panel is not None:
            panel.close()

        window.close()

        ok = state["listing_ok"] and state["sftp_ready"] and state["dialog_ready"] and state["forward_ok"]

        print("--- 判定 ---", flush=True)

        for name, value in state.items():
            print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

        print("SFTP_GUI_E2E_OK" if ok else "SFTP_GUI_E2E_FAILED", flush=True)

        app.quit()

    QTimer.singleShot(200, open_sftp)
    app.exec()

    return 0 if state["sftp_ready"] and state["forward_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
