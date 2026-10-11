"""重输出渲染验证：模拟 yum/docker 下载时的刷屏 + \\r 进度条，确认终端不黑屏。

对应真实反馈：在 Linux 上下载东西时终端一片黑（输出滚进历史后当前屏幕被推出视口）。

运行::

    .venv\\Scripts\\python.exe tests\\heavy_output_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-heavy-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow

# 逐行刷屏 + \r 原地刷新，复刻 yum/docker 的下载进度输出
REMOTE_COMMAND = (
    "for i in $(seq 1 400); do echo \"progress-line-$i\"; done; "
    "for p in $(seq 1 100); do printf \"\\rDownloading: $p%% [#####] 1.2 MB/s\"; sleep 0.01; done; "
    "printf \"\\r\\n\"; echo RENDER-CHECK-DONE"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = next(
        (item for item in context.hosts.hosts if item.hostname == args.host and item.port == args.port),
        None,
    )

    if host is None:
        host = Host(label=args.host or "test", hostname=args.host, port=args.port, username=args.username)
        context.hosts.add(host)

    host.verify_host_key = False
    context.hosts.update(host)

    if args.password:
        context.credentials.set(host.id, args.password)

    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()

    state = {"visible": False, "filled": False, "output_text": False, "not_black": False}
    deadline = time.time() + args.timeout
    build_dir = Path(__file__).resolve().parent.parent / "build"

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    pane = None

    def wait_for(predicate, timeout: float = 30.0, interval: float = 250) -> bool:
        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(interval / 1000.0)

        return predicate()

    def step_connect() -> None:
        nonlocal pane

        window.connect_host(host)

        def poll() -> None:
            nonlocal pane

            session = window._session_for(host.id)

            if session is None or session.session is None:
                if time.time() > deadline:
                    print("会话未建立", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(400, poll)
                return

            pane = session
            ready = wait_for(
                lambda: any(marker in pane.view.output_text() for marker in ("#", "$", ">")),
                timeout=30,
            )

            if not ready:
                print("没有看到 shell 提示符", flush=True)
                app.quit()
                return

            print("会话就绪，开始刷屏…", flush=True)
            pane.send_raw(REMOTE_COMMAND)
            QTimer.singleShot(1500, check)

        QTimer.singleShot(500, poll)

    def check() -> None:
        view = pane.view

        def finished() -> bool:
            return "RENDER-CHECK-DONE" in view.output_text()

        wait_for(finished, timeout=60)

        # 渲染检查：视口里必须能看到最新内容，且不能一片黑
        rows = [view._row_text(row) for row in range(view._viewport_rows())]
        filled = sum(1 for row in rows if row.strip())

        image = view.grab().toImage()
        background = view._theme["background"].lower()
        painted = 0
        total = 0

        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                total += 1

                if image.pixelColor(x, y).name().lower() != background:
                    painted += 1

        ratio = painted / total if total else 0.0

        state["output_text"] = "RENDER-CHECK-DONE" in view.output_text()
        state["visible"] = any("RENDER-CHECK-DONE" in row for row in rows) or any(
            "Downloading:" in row for row in rows
        )
        state["filled"] = filled >= len(rows) - 1
        state["not_black"] = ratio > 0.03

        print(
            f"视口 {len(rows)} 行，有内容 {filled} 行；非背景像素 {ratio:.1%}；"
            f"历史行数={len(view._history_top)}；光标在视口第 {view._view_row_for_screen_row(view.emulator.cursor.y)} 行",
            flush=True,
        )
        print(f"视口最后一行：{rows[-1]!r}", flush=True)

        view.grab().save(str(build_dir / "heavy-output.png"))
        window.grab().save(str(build_dir / "heavy-output-window.png"))

        finish()

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
    print("HEAVY_OUTPUT_E2E_OK" if ok else "HEAVY_OUTPUT_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
