"""端到端验证并抓真实屏幕：确认窗口上真的能看到终端文字。"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.session_tab import SessionTab

MARKER = "PYSSH_E2E_OK"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.206.130")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--trust", action="store_true")
    parser.add_argument("--timeout", type=float, default=40.0)
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
        context.credentials.set(host.id, args.password)

    if args.trust:
        from sshmanager.sshclient import save_host_key

        print("trusted:", save_host_key(args.host, args.port), flush=True)

    window = MainWindow(context)
    window.resize(1200, 760)
    window.show()

    state = {"sent": False, "done": False, "tab": None}
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    build = Path(__file__).resolve().parent.parent / "build"
    build.mkdir(exist_ok=True)

    def crop_terminal(image):
        """从整窗截图里裁出终端区域。"""

        tab = state["tab"]

        if tab is None:
            return image

        origin = tab.view.viewport().mapTo(window, tab.view.viewport().rect().topLeft())
        return image.copy(origin.x(), origin.y(), tab.view.viewport().width(), tab.view.viewport().height())

    def report(label, image, colors=6):
        counter = Counter()

        for y in range(0, image.height(), 2):
            for x in range(0, image.width(), 2):
                counter[image.pixelColor(x, y).name()] += 1

        print(f"{label}: {counter.most_common(colors)}", flush=True)

        return counter

    def on_data(_data: bytes) -> None:
        text = state["tab"].view.emulator.plain_text()

        if not state["sent"] and any(prompt in text for prompt in ("$", "#", ">", "]")):
            state["sent"] = True
            state["tab"].session.send(
                f"echo {MARKER}; printf '\\033[32mGREEN\\033[0m \\033[1;34mBLUE\\033[0m\\n'; "
                f"echo '中文渲染 · '$(uname -r)\r\n".encode()
            )

        if state["done"] or not state["sent"]:
            return

        if not any(line.strip() in ("GREEN BLUE", "GREEN  BLUE") for line in text.splitlines()):
            return

        state["done"] = True
        watchdog.stop()

        # 给窗口一点时间真正刷新到屏幕
        QTimer.singleShot(400, capture)

    def capture() -> None:
        window.raise_()
        window.activateWindow()
        app.processEvents()

        # 1) 整窗截图（Qt 自己渲染）
        window_shot = window.grab().toImage()
        window_shot.save(str(build / "e2e-window.png"))

        terminal_shot = crop_terminal(window_shot)
        terminal_shot.save(str(build / "e2e-terminal.png"))
        counter = report("窗口截图的终端区域", terminal_shot)

        # 2) 真实屏幕截图（用户肉眼所见）
        screen = QGuiApplication.primaryScreen()

        if screen is not None:
            screen_shot = screen.grabWindow(0).toImage()
            screen_shot.save(str(build / "e2e-screen.png"))
            geometry = window.frameGeometry()
            ratio = screen_shot.width() / max(1, screen.geometry().width())
            region = screen_shot.copy(
                int(geometry.x() * ratio),
                int(geometry.y() * ratio),
                int(geometry.width() * ratio),
                int(geometry.height() * ratio),
            )
            region.save(str(build / "e2e-screen-window.png"))
            report("真实屏幕截图（窗口区域）", region)

        print("绿色像素:", counter.get("#3fb950", 0), "蓝色像素:", counter.get("#58a6ff", 0), flush=True)
        print("screenshots:", build / "e2e-window.png", build / "e2e-screen-window.png", flush=True)

        QTimer.singleShot(200, app.quit)

    def attach() -> None:
        tab = window._session_for(host.id)

        if not isinstance(tab, SessionTab):
            QTimer.singleShot(50, attach)
            return

        state["tab"] = tab
        tab.session.data_received.connect(on_data)

    window.connect_host(host)
    QTimer.singleShot(0, attach)

    app.exec()

    tab = window._session_for(host.id)
    text = tab.view.emulator.plain_text() if tab else ""
    window.shutdown()

    from sshmanager.sshclient import close_all_clients
    from sshmanager.ui.session_tab import drain_sessions

    close_all_clients()
    drain_sessions(timeout=8)

    print("--- 终端末尾 ---", flush=True)

    for line in [line for line in text.splitlines() if line.strip()][-4:]:
        print(line, flush=True)

    ok = state["done"]
    print("E2E_OK" if ok else "E2E_FAILED", flush=True)

    # 跳过解释器退出阶段（PySide6 6.11 + Python 3.14 退出期可能因 C 层残留线程崩溃）
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
