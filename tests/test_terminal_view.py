"""终端控件的滚动与排版回归测试。

锁住两个真实踩过的坑：

1. 早期版本靠逐行比对猜「滚了几行」，每次输出都会累加 offset，
   最终把画面整个推出视口（终端一片空白）。
2. 渲染时把「屏幕行」按历史行数平移，且只绘制屏幕行——只要历史行数超过
   视口高度，当前屏幕就被推到视口下方，**整屏黑**（下载大文件时最容易遇到）。
   这里的 ``test_rendering_not_black_*`` 直接抓取渲染结果来兜住这个回归。
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


def _visible_rows(view: TerminalView) -> list[str]:
    """当前视口里能看到的文字（逐视口行）。"""

    return [view._row_text(row) for row in range(view._viewport_rows())]


def _non_background_ratio(view: TerminalView) -> float:
    """渲染结果里非背景色像素的比例——用来判断"是不是一片黑"。"""

    image = view.grab().toImage()
    background = view._theme["background"].lower()
    total = 0
    painted = 0

    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            total += 1

            if image.pixelColor(x, y).name().lower() != background:
                painted += 1

    return painted / total if total else 0.0


# ----------------------------------------------------------------------
# 偏移与滚动
# ----------------------------------------------------------------------

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
    assert "history-line-29" in _visible_rows(view)[-1] or any(
        "history-line-29" in row for row in _visible_rows(view)
    )


def test_new_output_keeps_scroll_position(view: TerminalView):
    """已经向上翻看历史时，新输出不应把画面顶走。"""

    for index in range(40):
        view.feed(f"old-{index}\r\n".encode())

    view.verticalScrollBar().setValue(0)  # 翻到最上面
    top_before = view._row_text(0)

    for index in range(10):
        view.feed(f"new-{index}\r\n".encode())

    assert view._offset > 0, "仍在回看历史"
    assert view._row_text(0) == top_before, "正在看的那一行不应被新输出挤走"


# ----------------------------------------------------------------------
# 排版：不足一屏从顶部开始，历史超屏时当前屏幕必须可见
# ----------------------------------------------------------------------

def test_content_starts_at_top_when_few_lines(view: TerminalView):
    """内容不足一屏时，画面应从**顶部**开始排（空白留在下方）。"""

    for index in range(3):
        view.feed(f"short-{index}\r\n".encode())

    rows = _visible_rows(view)

    assert "short-0" in rows[0], f"第一行应显示在视口顶部，实际是 {rows[0]!r}"
    assert view._first_view_row() == 0


def test_last_screen_row_inside_viewport(view: TerminalView):
    """当前屏幕的最后一行必须落在视口内（否则就是黑屏 bug）。"""

    for index in range(300):
        view.feed(f"line-{index}\r\n".encode())

    assert view._offset == 0
    assert view._view_row_for_screen_row(view.emulator.lines - 1) >= 0, "当前屏幕被推出了视口"


def test_rendering_not_black_after_heavy_output(view: TerminalView):
    """输出远超一屏后，渲染结果必须仍能看到内容（回归：历史超屏导致整屏黑）。"""

    for index in range(300):
        view.feed(f"download-progress-{index}\r\n".encode())

    rows = _visible_rows(view)
    assert any("download-progress-299" in row for row in rows), "最新输出应在视口里"

    filled = sum(1 for row in rows if row.strip())
    assert filled >= len(rows) - 1, f"视口应被内容填满，实际只有 {filled}/{len(rows)} 行有内容"

    ratio = _non_background_ratio(view)
    assert ratio > 0.02, f"画面几乎全黑（非背景像素只有 {ratio:.2%}）"


def test_rendering_not_black_with_carriage_return_progress(view: TerminalView):
    """yum/docker 那种 \\r 进度条刷屏后，画面同样不能黑。"""

    view.feed(b"Downloading packages:\r\n")

    for percent in range(0, 101):
        view.feed(f"\r({percent}%)  {percent} kB/s".encode())

    view.feed(b"\r\nComplete!\r\n")

    rows = _visible_rows(view)
    assert any("Complete!" in row for row in rows)
    assert _non_background_ratio(view) > 0.02


def test_output_text_covers_history(view: TerminalView):
    """导出/复制到的内容要包含历史行与当前屏幕。"""

    for index in range(50):
        view.feed(f"text-{index}\r\n".encode())

    text = view.output_text()

    assert "text-0" in text and "text-49" in text


def test_selection_survives_scrolling(view: TerminalView):
    """选区记的是内容行号，滚动后复制的仍是当初选中的文字。"""

    for index in range(40):
        view.feed(f"select-{index}\r\n".encode())

    view.verticalScrollBar().setValue(0)  # 翻到最早
    view._anchor = (0, 0)
    view._cursor_cell = (0, 20)

    copied_early = view.selected_text()
    view.verticalScrollBar().setValue(view._max_offset())  # 回到最新

    assert "select-0" in copied_early
    assert view.selected_text() == copied_early, "滚动不应改变已选内容"
