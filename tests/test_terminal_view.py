"""终端控件的滚动与排版回归测试。

这两个用例锁住一个真实踩过的坑：早期版本靠逐行比对猜「滚了几行」，
每次输出都会误判并累加 offset，最后把画面整个推出视口（终端一片空白）。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sshmanager.terminal import TerminalView  # noqa: E402

_app: QApplication | None = None


def _application() -> QApplication:
    """整个测试会话只创建一个 QApplication。"""

    global _app

    if _app is None:
        _app = QApplication.instance() or QApplication([])

    return _app


@pytest.fixture()
def view() -> TerminalView:
    _application()

    widget = TerminalView(columns=40, rows=5)
    widget.resize(400, 100)
    widget.show()

    return widget


def test_offset_stays_zero_while_output_fits(view: TerminalView):
    """输出没超出屏幕时，视觉偏移必须保持为 0。"""

    for index in range(5):
        view.feed(f"line{index}\r\n".encode())

    assert view._offset == 0, "输出未溢出时不应发生滚动"


def test_scroll_up_shows_history(view: TerminalView):
    """内容超出屏幕后向上滚动，应该能看到更早的行，且不越界。"""

    for index in range(30):
        view.feed(f"history-line-{index}\r\n".encode())

    maximum = view._max_offset()

    assert maximum > 0, "超出一屏后应有可回看的历史"

    view.verticalScrollBar().setValue(0)  # 滚到最上面

    assert 0 < view._offset <= maximum
    assert "history-line-0" in view._row_text(0)

    view.verticalScrollBar().setValue(maximum)  # 回到最下面

    assert view._offset == 0


def test_bottom_aligned_when_few_lines(view: TerminalView):
    """内容不足一屏时，画面应从底部开始排（与真实终端一致）。"""

    view.resize(400, 100)
    view.emulator.resize(view.emulator.columns, 3)  # 故意留出多余空白行
    view.feed(b"only line\r\n")

    cell_height = view._cell.height()
    shift = view.viewport().height() // cell_height - 3

    assert shift >= 1, "夹具应当留下空白行"

    top = (0 + len(view._history_top) - view._offset + shift) * cell_height

    assert top >= shift * cell_height, "第一行不应贴在视口顶部"
