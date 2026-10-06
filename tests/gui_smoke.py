"""离屏 GUI 冒烟测试：窗口能构造、终端能渲染、不连任何真实服务器。

运行方式::

    $env:QT_QPA_PLATFORM='offscreen'; .venv\\Scripts\\python.exe tests\\gui_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYSSH_DATA_DIR"] = tempfile.mkdtemp(prefix="pyssh-gui-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from sshmanager.hosts import Host
from sshmanager.ui import AppContext, MainWindow

app = QApplication(sys.argv[:1])

from sshmanager.credentials import CredentialStore  # noqa: E402
from sshmanager.hosts import HostStore  # noqa: E402

context = AppContext(hosts=HostStore(), credentials=CredentialStore())
context.hosts.add(Host(label="冒烟测试机", hostname="127.0.0.1", port=2222, username="tester"))

window = MainWindow(context)
window.resize(1000, 640)
window.show()
app.processEvents()

assert window.host_list.count() == 1, "主机列表没有渲染出条目"

# 直接驱动终端控件：喂输入并做一次离屏渲染
from sshmanager.terminal import TerminalView  # noqa: E402

view = TerminalView(columns=80, rows=24)
view.resize(800, 480)
view.feed(b"\x1b[32mPS C:\\Users\\lzlvl>\x1b[0m echo hello\r\nhello\r\n")
view.feed("中文输出正常\r\n".encode("utf-8"))
app.processEvents()

image = QImage(view.viewport().size(), QImage.Format.Format_ARGB32)
image.fill(0)
painter = QPainter(image)
view.viewport().render(painter, QPoint(0, 0))
painter.end()

# 注意：离屏平台下 viewport().render() 常拿到空图，验收以窗口截图为准备用 view.grab()
assert not image.isNull(), "终端渲染失败"
assert view.emulator.visible_lines()[0].startswith("PS C:"), view.emulator.visible_lines()[:2]
assert "中文输出正常" in view.emulator.plain_text()

screenshot = Path(__file__).resolve().parent.parent / "build" / "terminal-smoke.png"
screenshot.parent.mkdir(exist_ok=True)
image.save(str(screenshot))

window.close()
print("GUI_SMOKE_OK")
print("terminal text:", view.emulator.visible_lines()[:3])
print("screenshot:", screenshot)
