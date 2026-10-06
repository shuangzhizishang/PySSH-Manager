"""真实 SSH 冒烟测试（单独运行，不参与 pytest 收集）。

前置条件：默认目标 192.168.206.130:22 可达，root 密码已保存在凭据库中
（首次由 main.py 的 LEGACY_PASSWORD 导入，或用 --password 参数传入）。

运行::

    $env:QT_QPA_PLATFORM='offscreen'
    .venv\\Scripts\\python.exe tests\\ssh_smoke.py --host 192.168.206.130 --username root
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host
from sshmanager.terminal import TerminalView

MARKER = "PYSSH_SMOKE_OK"
COLOR_MARKER = "PYSSH_COLOR_OK"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.206.130")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--trust", action="store_true", help="把服务器指纹写入 known_hosts 后连接")
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])

    host = Host(label="冒烟", hostname=args.host, port=args.port, username=args.username)

    if args.trust:
        from sshmanager.sshclient import save_host_key

        print("trusted fingerprint:", save_host_key(args.host, args.port))
    password = args.password

    if not password:
        store = CredentialStore()

        for key in list(store._cache):  # noqa: SLF001 - 冒烟脚本直接取第一条
            password = store.get(key) or ""
            break

    if not password:
        print("NO_CREDENTIAL: 凭据库里没有密码，请加 --password")
        return 2

    view = TerminalView(columns=100, rows=30)
    view.resize(900, 500)
    view.show()

    state = {"sent": False, "done": False, "text": "", "screenshot": None}

    def grab_screenshot() -> None:
        view.repaint()
        app.processEvents()

        image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        view.viewport().render(painter, QPoint(0, 0))
        painter.end()

        # 统计非背景像素，确认真的画上东西了
        painted = 0

        for y in range(0, image.height(), 3):
            for x in range(0, image.width(), 3):
                if image.pixelColor(x, y).name() != "#0d1117":
                    painted += 1

        print(
            f"debug: viewport={view.viewport().size().width()}x{view.viewport().size().height()} "
            f"cell={view._cell.width()}x{view._cell.height()} "
            f"grid={view.emulator.columns}x{view.emulator.lines} "
            f"painted_samples={painted} text_len={len(view.emulator.plain_text())}",
            flush=True,
        )

        state["screenshot"] = image

    def on_data(data: bytes) -> None:
        view.feed(data)

        text = view.emulator.plain_text()
        state["text"] = text

        if not state["sent"] and any(prompt in text for prompt in ("$", "#", ">", "]")):
            print("prompt seen, sending command", flush=True)
            view.data_to_send.emit(
                f"echo {MARKER}; printf '\\033[32m{COLOR_MARKER}\\033[0m\\n'; "
                f"echo -e '\\033[1;34mbluetooth\\033[0m  \\033[33mwifi\\033[0m  \\033[36mssh\\033[0m'; "
                f"echo '中文渲染测试 · $(uname -r)'; exit\r\n".encode()
            )
            state["sent"] = True

        if MARKER in text and COLOR_MARKER in text and not state["done"]:
            state["done"] = True
            # 会话退出会清屏，所以在这一刻抓图
            grab_screenshot()
            timeout.stop()
            session.request_stop()
            session.wait(3000)
            QTimer.singleShot(50, app.quit)

    # 直接复用 SessionTab 的线程模型，确保测的是真实链路
    from sshmanager.session import SSHSession

    session = SSHSession(host, credential=password)
    session.data_received.connect(on_data)

    def direct_send(payload: bytes) -> None:
        print("direct_send:", payload[:60], flush=True)
        session.send(payload)

    view.data_to_send.connect(direct_send)
    session.failed.connect(lambda message: (print("SSH_FAILED:", message), app.quit()))
    session.start()

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(app.quit)
    timeout.start(int(args.timeout * 1000))

    app.exec()

    session.request_stop()
    session.wait(3000)

    text = state["text"]
    lines = [line for line in text.splitlines() if line.strip()]

    print("--- 终端输出（末尾 8 行）---")
    for line in lines[-8:]:
        print(line)

    print("--- 判定 ---")
    print("connected_data:", bool(text))
    print("command_executed:", MARKER in text)
    print("ansi_color_parsed:", COLOR_MARKER in text)

    # 找到彩色那行的属性，确认 SGR 被 pyte 解析成绿色
    green_found = False

    for row, column, char, style in view.emulator.iter_cells():
        if style.fg == "green":
            green_found = True
            break

    print("green_cell_present:", green_found)

    image = state["screenshot"]
    screenshot = Path(__file__).resolve().parent.parent / "build" / "ssh-smoke.png"
    screenshot.parent.mkdir(exist_ok=True)

    if image is not None:
        image.save(str(screenshot))
        print("screenshot:", screenshot)
    else:
        print("screenshot: 未捕获（标记未出现）")

    ok = state["done"] and green_found
    print("SSH_SMOKE_OK" if ok else "SSH_SMOKE_FAILED")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
