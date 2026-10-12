"""双主机分屏的真实验证：左边一台虚拟机、右边另一台（上下同理）。

同时验证这种分屏下「同步输入」仍然生效（两边一起执行同一条命令）。

    .venv\\Scripts\\python.exe tests\\two_host_split_e2e.py --host <A> --host2 <B> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-2host-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.split_tab import SplitTerminalTab

MARKER = "SPLIT-MARKER"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--host2", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    hosts = []

    for address in (args.host, args.host2):
        host = next((item for item in context.hosts.hosts if item.hostname == address), None)

        if host is None:
            host = Host(label=address, hostname=address, port=args.port, username="root")
            context.hosts.add(host)

        host.verify_host_key = False
        context.hosts.update(host)
        context.credentials.set(host.id, args.password)
        hosts.append(host)

    window = MainWindow(context)
    window.resize(1280, 820)
    window.show()

    build_dir = Path(__file__).resolve().parent.parent / "build"
    deadline = time.time() + args.timeout
    state = {
        "split_created": False,
        "left_is_first_host": False,
        "right_is_second_host": False,
        "both_vms_alive": False,
        "orientation_vertical": False,
        "sync_in_split": False,
    }

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

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

    def step() -> None:
        window.open_split_hosts(hosts[0], hosts[1], Qt.Orientation.Horizontal)

        def poll() -> None:
            split = window.tabs_widget.currentWidget()

            if not isinstance(split, SplitTerminalTab) or len(split.panes) != 2:
                if time.time() > deadline:
                    print("分屏没建起来", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(300, poll)
                return

            panes = split.panes
            ready = wait_for(
                lambda: all(any(mark in text_of(pane) for mark in ("#", "$", ">")) for pane in panes),
                timeout=50,
            )

            state["split_created"] = len(split.panes) == 2 and split.splitter.orientation() == Qt.Orientation.Horizontal
            state["left_is_first_host"] = panes[0].host.id == hosts[0].id
            state["right_is_second_host"] = panes[1].host.id == hosts[1].id

            # 两个面板各自执行 hostname，证明连的是不同虚拟机
            for pane in panes:
                pane.send_raw(f"echo {MARKER}-$(hostname)")

            wait_for(
                lambda: all(MARKER in text_of(pane) for pane in panes),
                timeout=40,
            )

            left_lines = [line for line in text_of(panes[0]).splitlines() if MARKER in line]
            right_lines = [line for line in text_of(panes[1]).splitlines() if MARKER in line]
            state["both_vms_alive"] = bool(left_lines and right_lines and left_lines != right_lines)

            print(f"1. 左右分屏：面板 {len(split.panes)} 个，方向 {split.splitter.orientation().name}")
            print(f"   左 = {panes[0].host.display_name}（{panes[0].host.hostname}）→ {left_lines[-1:]}")
            print(f"   右 = {panes[1].host.display_name}（{panes[1].host.hostname}）→ {right_lines[-1:]}")
            print(f"   标签标题：{window.tabs_widget.tabText(window.tabs_widget.currentIndex())}")

            window.grab().save(str(build_dir / "split-two-hosts.png"))

            QTimer.singleShot(300, step_sync)

        QTimer.singleShot(400, poll)

    # ------------------------------------------------------------------
    # 分屏里开启同步输入：两边一起执行
    # ------------------------------------------------------------------
    def step_sync() -> None:
        split = window.tabs_widget.currentWidget()
        panes = split.panes

        window.action_sync_input.setChecked(True)
        app.processEvents()
        panes[0].view.setFocus()
        app.processEvents()

        panes[0].view.data_to_send.emit(f"echo {MARKER}-SYNC-$(hostname)\r".encode())

        ok = wait_for(
            lambda: all(f"{MARKER}-SYNC" in text_of(pane) for pane in panes),
            timeout=40,
        )
        state["sync_in_split"] = ok
        print(f"2. 分屏内同步输入：两个面板都执行={ok}", flush=True)

        window.action_sync_input.setChecked(False)
        app.processEvents()

        QTimer.singleShot(300, step_vertical)

    # ------------------------------------------------------------------
    # 上下分屏：保持两台主机，只旋转方向
    # ------------------------------------------------------------------
    def step_vertical() -> None:
        split = window.tabs_widget.currentWidget()
        window.split_current(Qt.Orientation.Vertical)
        app.processEvents()

        state["orientation_vertical"] = (
            split.splitter.orientation() == Qt.Orientation.Vertical
            and len(split.panes) == 2
            and split.panes[0].host.id == hosts[0].id
            and split.panes[1].host.id == hosts[1].id
        )

        print(
            f"3. 上下分屏：方向 {split.splitter.orientation().name}，面板 {len(split.panes)} 个，"
            f"主机 {[pane.host.hostname for pane in split.panes]}",
            flush=True,
        )

        window.grab().save(str(build_dir / "split-two-hosts-vertical.png"))
        finish()

    def finish() -> None:
        watchdog.stop()
        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("TWO_HOST_SPLIT_E2E_OK" if ok else "TWO_HOST_SPLIT_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
