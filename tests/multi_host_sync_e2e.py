"""多主机同步输入 + 分屏上限的真实链路验证。

对应需求：开两个主机的会话，在一个窗口输入命令，两个主机都执行（方便批量配环境）；
左右分屏只要两个面板。

运行::

    .venv\\Scripts\\python.exe tests\\multi_host_sync_e2e.py --host <主机A> --host2 <主机B> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-sync-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.split_tab import SplitTerminalTab

MARKER_A = "SYNC-MARKER-ONE"
MARKER_B = "SYNC-MARKER-SECOND"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--host2", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    hosts = []

    for address in (args.host, args.host2):
        host = next((item for item in context.hosts.hosts if item.hostname == address), None)

        if host is None:
            host = Host(label=address, hostname=address, port=args.port, username=args.username)
            context.hosts.add(host)

        host.verify_host_key = False
        context.hosts.update(host)
        context.credentials.set(host.id, args.password)
        hosts.append(host)

    window = MainWindow(context)
    window.resize(1280, 820)
    window.show()

    state = {
        "two_sessions": False,
        "sync_executes_on_both": False,
        "sync_off_stops_broadcast": False,
        "split_max_two": False,
    }

    deadline = time.time() + args.timeout
    build_dir = Path(__file__).resolve().parent.parent / "build"

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    panes: list = []

    def wait_for(predicate, timeout: float = 45.0, interval: float = 200) -> bool:
        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(interval / 1000.0)

        return predicate()

    def text_of(pane) -> str:
        return pane.view.emulator.plain_text()

    # ------------------------------------------------------------------

    def step_connect() -> None:
        for host in hosts:
            window.connect_host(host, force_new=True)

        def poll() -> None:
            nonlocal panes

            panes = [window._session_for(host.id) for host in hosts]

            if any(pane is None or pane.session is None for pane in panes):
                if time.time() > deadline:
                    print("会话未建立", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(400, poll)
                return

            ready = wait_for(
                lambda: all(any(mark in text_of(pane) for mark in ("#", "$", ">")) for pane in panes),
                timeout=40,
            )

            state["two_sessions"] = ready and len(panes) == 2
            print(
                f"1. 两个主机会话：{ready}（{hosts[0].hostname} / {hosts[1].hostname}）",
                flush=True,
            )

            QTimer.singleShot(300, step_enable_sync)

        QTimer.singleShot(500, poll)

    # ------------------------------------------------------------------
    # 1) 开启同步输入，在一个窗口输入命令，两个主机都执行
    # ------------------------------------------------------------------
    def step_enable_sync() -> None:
        window.action_sync_input.setChecked(True)
        app.processEvents()

        first, second = panes
        first.view.setFocus()
        app.processEvents()

        # 用真实的输入路径：终端控件把按键交给会话（会触发 input_generated 广播）
        first.view.data_to_send.emit(f"echo {MARKER_A}-$(hostname)\r".encode())

        ok = wait_for(
            lambda: MARKER_A in text_of(first) and MARKER_A in text_of(second),
            timeout=45,
        )

        first_text = text_of(first)
        second_text = text_of(second)

        # 两个主机各自执行一次 → 回显里的主机名不同，证明是"各自执行"而不是复制的文本
        distinct = MARKER_A in first_text and MARKER_A in second_text and first_text != second_text

        state["sync_executes_on_both"] = ok and distinct
        print(f"2. 同步输入：两台都执行={ok}，回显不同（各自执行）={distinct}", flush=True)
        print(f"   {hosts[0].hostname}: {[line for line in first_text.splitlines() if MARKER_A in line][-1:]}", flush=True)
        print(f"   {hosts[1].hostname}: {[line for line in second_text.splitlines() if MARKER_A in line][-1:]}", flush=True)

        window.grab().save(str(build_dir / "sync-input.png"))

        QTimer.singleShot(300, step_sync_off)

    # ------------------------------------------------------------------
    # 2) 关闭同步后，命令只在那一个会话执行
    # ------------------------------------------------------------------
    def step_sync_off() -> None:
        window.action_sync_input.setChecked(False)
        app.processEvents()

        first, second = panes
        before_second = text_of(second)

        first.view.data_to_send.emit(f"echo {MARKER_B}-only-one\r".encode())
        wait_for(lambda: MARKER_B in text_of(first), timeout=30)
        app.processEvents()
        time.sleep(1.0)
        app.processEvents()

        leaked = MARKER_B in text_of(second)
        state["sync_off_stops_broadcast"] = MARKER_B in text_of(first) and not leaked
        print(f"3. 关闭同步：只在本会话执行={MARKER_B in text_of(first)}，另一台没收到={not leaked}", flush=True)

        QTimer.singleShot(300, step_split_limit)

    # ------------------------------------------------------------------
    # 3) 分屏最多两个面板
    # ------------------------------------------------------------------
    def step_split_limit() -> None:
        window.tabs_widget.setCurrentWidget(panes[0])
        app.processEvents()

        window.split_current(Qt_orientation())
        app.processEvents()

        split = window.tabs_widget.currentWidget()

        if not isinstance(split, SplitTerminalTab):
            print("分屏没有创建成功", flush=True)
            app.quit()
            return

        split.add_pane(activate=False)  # 第三次尝试加面板
        app.processEvents()
        panes_now = len(split.panes)

        state["split_max_two"] = panes_now == 2 and SplitTerminalTab.MAX_PANES == 2
        print(f"4. 分屏面板数：{panes_now}（上限 {SplitTerminalTab.MAX_PANES}）", flush=True)

        window.grab().save(str(build_dir / "split-two.png"))
        finish()

    def Qt_orientation():  # noqa: N802 - 就近使用
        from PySide6.QtCore import Qt

        return Qt.Orientation.Horizontal

    def finish() -> None:
        watchdog.stop()
        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step_connect)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("MULTI_HOST_SYNC_E2E_OK" if ok else "MULTI_HOST_SYNC_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
