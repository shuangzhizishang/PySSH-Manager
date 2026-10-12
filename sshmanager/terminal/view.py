"""Qt 终端控件：字符网格渲染 + 键盘输入编码 + 滚动缓冲。"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QAbstractScrollArea

from .core import KEY_SEQUENCES, CellStyle, Emulator, ctrl_byte
from ..settings import trace_ui


def _trace_enabled() -> bool:
    import os

    return os.environ.get("PYSSH_TRACE_UI", "").strip() in ("1", "true", "True", "yes")

# ----------------------------------------------------------------------
# 配色
# ----------------------------------------------------------------------

THEME = {
    "background": "#0d1117",
    "foreground": "#e6edf3",
    "cursor": "#e6edf3",
    "selection": "#264f78",
    "black": "#0d1117",
    "red": "#ff7b72",
    "green": "#3fb950",
    "brown": "#d29922",
    "blue": "#58a6ff",
    "magenta": "#bc8cff",
    "cyan": "#39c5cf",
    "white": "#b1bac4",
    "brightblack": "#6e7681",
    "brightred": "#ffa198",
    "brightgreen": "#56d364",
    "brightbrown": "#e3b341",
    "brightblue": "#79c0ff",
    "brightmagenta": "#d2a8ff",
    "brightcyan": "#56d4dd",
    "brightwhite": "#f0f6fc",
}

#: 终端常用字体，按优先级挑选
PREFERRED_FONTS = (
    "Cascadia Mono",
    "Cascadia Code",
    "JetBrains Mono",
    "Sarasa Mono SC",
    "Consolas",
    "DejaVu Sans Mono",
    "Courier New",
)


class TerminalView(QAbstractScrollArea):
    """渲染 :class:`Emulator` 的画面，并把用户输入编码成字节序列。"""

    #: 用户输入（已编码）
    data_to_send = Signal(bytes)

    #: 终端尺寸变化（列, 行）
    terminal_resized = Signal(int, int)

    #: 标题变化（OSC 0/2）
    title_changed = Signal(str)

    #: 有内容被复制到 Windows 剪贴板（Linux→Windows）
    copied = Signal(str)

    def __init__(self, columns: int = 120, rows: int = 32, parent=None) -> None:
        super().__init__(parent)

        self.emulator = Emulator(columns=columns, lines=rows)

        self._theme = dict(THEME)
        self._font = self._pick_font(None, 12)
        self._cell = self._measure(self._font)

        #: 鼠标松开时自动把选中内容复制到 Windows 剪贴板（PuTTY 风格）
        self.select_to_copy = True

        # 绘制缓存：颜色与字体按样式键复用，避免每格新建 QColor/QFont
        self._color_cache: dict[str, QColor] = {}
        self._font_cache: dict[tuple, QFont] = {}

        # 历史行列表快照（deque 按下标取值很慢，见 _history_list）
        self._history_snapshot: list = []
        self._history_len = -1

        # 回滚：offset 表示视口相对「虚拟轴末尾」向上偏移的行数
        self._offset = 0
        self._last_total = self.emulator.lines

        # 选区
        self._anchor: tuple[int, int] | None = None
        self._cursor_cell: tuple[int, int] | None = None
        self._selecting = False

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAttribute(Qt.WidgetAttribute.WA_KeyCompression, False)

        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._sync_scrollbar()
        self._last_title = ""

    # ------------------------------------------------------------------
    # 字体 / 主题
    # ------------------------------------------------------------------

    def _pick_font(self, family: str | None, size: int) -> QFont:
        available = set(QFontDatabase.families())

        chosen = family if family in available else None

        if chosen is None:
            for candidate in PREFERRED_FONTS:
                if candidate in available:
                    chosen = candidate
                    break

        font = QFont(chosen or "Courier New")
        font.setPointSize(max(6, int(size)))
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        font.setKerning(False)

        return font

    @staticmethod
    def _measure(font: QFont) -> QSize:
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance("M")

        if width <= 0:
            width = metrics.averageCharWidth()

        height = metrics.lineSpacing()

        return QSize(
            max(1, int(round(width))),
            max(1, int(round(height))),
        )

    def apply_font(self, family: str | None, size: int) -> None:
        self._font = self._pick_font(family, size)
        self._cell = self._measure(self._font)
        self._font_cache.clear()
        self._update_size_hint()
        self.viewport().update()

    def apply_theme(self, theme: dict) -> None:
        self._theme.update(theme)
        self._cleared_caches()
        self.viewport().update()

    def focusInEvent(self, event):  # noqa: N802 - Qt 命名
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event):  # noqa: N802
        super().focusOutEvent(event)
        self.viewport().update()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def feed(self, data: bytes | str) -> None:
        """喂入服务器输出。"""

        trace = _trace_enabled()
        started = time.perf_counter() if trace else 0.0

        self.emulator.feed(data)

        parsed = time.perf_counter() if trace else 0.0
        title = self.emulator.title

        if title != self._last_title:
            self._last_title = title
            self.title_changed.emit(title)

        self._track_output()
        self.viewport().update()

        if trace:
            total = time.perf_counter() - started
            parse = parsed - started
            scroll = time.perf_counter() - parsed

            # 只报"明显拖慢界面"的调用
            if total > 0.05:
                trace_ui(
                    f"feed {len(data)}B：解析 {parse * 1000:.0f}ms，滚动 {scroll * 1000:.0f}ms，"
                    f"合计 {total * 1000:.0f}ms"
                )

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._apply_grid_size()
        self.viewport().update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(
            self.emulator.columns * self._cell.width(),
            self.emulator.lines * self._cell.height(),
        )

    # ------------------------------------------------------------------
    # 尺寸同步
    # ------------------------------------------------------------------

    def _apply_grid_size(self) -> None:
        columns = max(20, self.viewport().width() // self._cell.width())
        rows = max(5, self.viewport().height() // self._cell.height())

        if (columns, rows) == (self.emulator.columns, self.emulator.lines):
            return

        if self.emulator.resize(columns, rows):
            self.terminal_resized.emit(columns, rows)

    def _update_size_hint(self) -> None:
        self.updateGeometry()

    # ------------------------------------------------------------------
    # 滚动
    #
    # 坐标模型：把「回滚历史 + 当前屏幕 + 底部历史」看成一条**虚拟行轴**，
    # 视口只是这条轴上的一个窗口：
    #
    #     虚拟行 0  ────────────  history.top（越往上越旧）
    #               ────────────  当前屏幕（emulator.lines 行）
    #               ────────────  history.bottom
    #     最后一行  ────────────  最新输出
    #
    # ``_offset`` = 视口底部距离虚拟轴末尾多少行：0 表示贴底（跟随最新输出）。
    #
    # 旧实现直接用「屏幕行 + 历史行数」当 y 坐标，且只绘制屏幕行——
    # 一旦历史行数超过视口高度，当前屏幕就被推到视口下方，终端整屏黑
    # （下载大文件时输出滚得快，最容易触发）。
    # ------------------------------------------------------------------

    @property
    def _history_top(self):
        return self.emulator.screen.history.top

    def _history_list(self) -> list:
        """历史行列表快照。

        pyte 的 history.top 是 deque，**按下标访问是 O(n)**；历史几千行时
        每帧绘制都要按行号取值，会把重绘拖慢十几倍。这里缓存成 list，
        只在长度变化时重建一次。
        """

        top = self._history_top
        length = len(top)

        if length != self._history_len:
            self._history_snapshot = list(top)
            self._history_len = length

        return self._history_snapshot

    @property
    def _history_bottom(self):
        return self.emulator.screen.history.bottom

    def _total_lines(self) -> int:
        """虚拟轴总行数。"""

        return len(self._history_top) + self.emulator.lines + len(self._history_bottom)

    def _viewport_rows(self) -> int:
        """视口能显示多少行。"""

        viewport = self.viewport()
        height = viewport.height() if viewport is not None else self.height()

        return max(1, int(height // max(1, self._cell.height())))

    def _max_offset(self) -> int:
        """最多能向上回看多少行（内容不足一屏时为 0）。"""

        return max(0, self._total_lines() - self._viewport_rows())

    def _first_view_row(self) -> int:
        """视口第一行对应的虚拟行号。

        内容不足一屏时返回 0：画面从**顶部**开始排，多余空白留在下方
        （真实终端就是这样，不能把内容顶到视口底部）。
        """

        return max(0, self._total_lines() - self._offset - self._viewport_rows())

    def _virtual_at_view(self, view_row: int) -> int:
        return self._first_view_row() + view_row

    def _sync_scrollbar(self) -> None:
        bar = self.verticalScrollBar()
        maximum = self._max_offset()

        self._offset = max(0, min(self._offset, maximum))

        bar.blockSignals(True)
        bar.setRange(0, maximum)
        bar.setPageStep(max(1, self._viewport_rows()))
        bar.setValue(maximum - self._offset)
        bar.blockSignals(False)

    def _on_scroll(self, value: int) -> None:
        self._offset = self._max_offset() - value
        self.viewport().update()

    def _track_output(self) -> None:
        """新输出到达后的滚动处理。

        贴底时继续跟随最新输出；已经向上翻看历史时，保持正在看的那段内容不动
        （否则输出一多，画面就会自己滑走）。
        """

        total = self._total_lines()
        delta = total - self._last_total
        self._last_total = total

        if delta > 0 and self._offset > 0:
            self._offset = min(self._max_offset(), self._offset + delta)

        self._sync_scrollbar()

    # ------------------------------------------------------------------
    # 按虚拟行取内容
    # ------------------------------------------------------------------

    @staticmethod
    def _cell_from_char(char) -> tuple[str, CellStyle] | None:
        if char is None:
            return None

        return char.data, CellStyle(
            fg=char.fg,
            bg=char.bg,
            bold=char.bold,
            italics=char.italics,
            underscore=char.underscore,
            strikethrough=char.strikethrough,
            reverse=char.reverse,
        )

    def _history_line(self, index: int):
        """历史行（top 之后接 bottom），越界返回 None。"""

        top = self._history_list()

        if 0 <= index < len(top):
            return top[index]

        bottom_index = index - (len(top) + self.emulator.lines)
        bottom = self._history_bottom

        if 0 <= bottom_index < len(bottom):
            return bottom[bottom_index]

        return None

    def _line_text(self, virtual: int) -> str:
        """虚拟行上的纯文本（越界返回空串）。"""

        columns = self.emulator.columns
        top = self._history_list()

        if 0 <= virtual < len(top):
            line = top[virtual]

            return "".join(line[column].data if column in line else " " for column in range(columns)).rstrip()

        screen_start = len(top)

        if screen_start <= virtual < screen_start + self.emulator.lines:
            lines = self.emulator.visible_lines()
            index = virtual - screen_start

            return lines[index].rstrip() if 0 <= index < len(lines) else ""

        line = self._history_line(virtual)

        if line is None:
            return ""

        return "".join(line[column].data if column in line else " " for column in range(columns)).rstrip()

    def _cell_at_virtual(self, virtual: int, column: int) -> tuple[str, CellStyle] | None:
        """虚拟行某列的字符与属性（历史行同样带属性，pyte 会保留配色）。"""

        if not (0 <= column < self.emulator.columns):
            return None

        top = self._history_list()
        screen_start = len(top)

        if virtual < screen_start:
            return self._cell_from_char(top[virtual].get(column)) if virtual >= 0 else None

        if virtual < screen_start + self.emulator.lines:
            line = self.emulator.screen.buffer.get(virtual - screen_start)

            return self._cell_from_char(line.get(column)) if line is not None else None

        line = self._history_line(virtual)

        return self._cell_from_char(line.get(column)) if line is not None else None

    def _cell_from_history(self, line, column: int):
        """兼容旧接口：从历史行取单元格。"""

        return self._cell_from_char(line.get(column) if line is not None else None)

    # --- 视口坐标：绘制 / 命中测试 / 选区统一用「视口第几行」 ---

    def _row_text(self, view_row: int) -> str:
        """视口第 view_row 行显示的文字（0 = 视口最上面一行）。"""

        return self._line_text(self._virtual_at_view(view_row))

    def _is_history_row(self, view_row: int) -> bool:
        """该视口行是否来自回滚历史。"""

        return self._virtual_at_view(view_row) < len(self._history_top)

    def _cell_at(self, view_row: int, column: int) -> tuple[str, CellStyle] | None:
        """取视口坐标 (view_row, column) 处的字符与属性。"""

        return self._cell_at_virtual(self._virtual_at_view(view_row), column)

    def _view_row_for_screen_row(self, screen_row: int) -> int:
        """当前屏幕的第 screen_row 行落在视口第几行；不在视口内返回 -1。"""

        if not (0 <= screen_row < self.emulator.lines):
            return -1

        view_row = len(self._history_top) + screen_row - self._first_view_row()

        return view_row if 0 <= view_row < self._viewport_rows() else -1

    def wheelEvent(self, event):  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = 1 if event.angleDelta().y() > 0 else -1
            self._zoom(delta)
            return

        super().wheelEvent(event)

    def _zoom(self, delta: int) -> None:
        size = self._font.pointSize() + delta
        self.apply_font(self._font.family(), max(6, min(48, size)))

    # ------------------------------------------------------------------
    # 颜色
    # ------------------------------------------------------------------

    def _resolve_color(self, name: str) -> QColor:
        cached = self._color_cache.get(name)

        if cached is not None:
            return cached

        theme = self._theme
        color = None

        if name in theme:
            color = QColor(theme[name])
        elif name.startswith("#") and len(name) in (4, 7):
            color = QColor(name)
        else:
            palette = {
                "black": "black",
                "red": "red",
                "green": "green",
                "brown": "yellow",
                "blue": "blue",
                "magenta": "magenta",
                "cyan": "cyan",
                "white": "white",
            }

            if name in palette:
                color = QColor(palette[name])
            elif name.startswith("bright") and name[6:] in palette:
                color = QColor(palette[name[6:]]).lighter(125)

        if color is None:
            color = QColor(theme["foreground"])

        self._color_cache[name] = color

        return color

    def _cleared_caches(self) -> None:
        """字体/颜色/主题变化后清缓存。"""

        self._color_cache.clear()
        self._font_cache.clear()

    def _font_for_key(self, key: tuple) -> QFont:
        """按样式键取字体（缓存，避免每格新建 QFont）。"""

        font = self._font_cache.get(key)

        if font is not None:
            return font

        _, _, bold, italics, underscore, strikethrough, _ = key
        font = QFont(self._font)
        font.setBold(bool(bold))
        font.setItalic(bool(italics))
        font.setUnderline(bool(underscore))
        font.setStrikeOut(bool(strikethrough))
        self._font_cache[key] = font

        return font

    def _color_for_key(self, key: tuple) -> QColor:
        fg, bg, _, _, _, _, reverse = key

        return self._resolve_color(bg if reverse else fg)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _raw_line(self, virtual: int):
        """虚拟行对应的 pyte 行对象（只含写入过的单元格）；越界返回 None。"""

        top = self._history_list()

        if 0 <= virtual < len(top):
            return top[virtual]

        screen_start = len(top)

        if screen_start <= virtual < screen_start + self.emulator.lines:
            return self.emulator.screen.buffer.get(virtual - screen_start)

        return self._history_line(virtual)

    @staticmethod
    def _style_key(char) -> tuple:
        """样式键：同样式键的连续单元格可以合并成一次 drawText。"""

        return (
            char.fg,
            char.bg,
            bool(char.bold),
            bool(char.italics),
            bool(char.underscore),
            bool(char.strikethrough),
            bool(char.reverse),
        )

    def _paint_line(self, painter, line, top: int, baseline: int, cell_width: int, cell_height: int) -> None:
        """画一行：先铺该行有背景色的格子，再按同样式合并文本片段。

        只遍历"写入过的单元格"（pyte 的行是稀疏字典），刷屏时比逐列扫描快很多。
        """

        runs: list[list] = []
        current = None

        for column, char in line.items():
            key = self._style_key(char)

            # 背景色（含反显：反显时用前景色铺底）
            if char.reverse or char.bg not in ("default", ""):
                painter.fillRect(
                    QRect(column * cell_width, top, cell_width, cell_height),
                    self._resolve_color(char.fg if char.reverse else char.bg),
                )

            # 纯空格且无背景：不必画字
            if char.data == " " and char.bg in ("default", "") and not char.reverse:
                continue

            if current is not None and current[0] == key and current[1] + len(current[2]) == column:
                current[2] += char.data
            else:
                if current is not None:
                    runs.append(current)

                current = [key, column, char.data]

        if current is not None:
            runs.append(current)

        for key, column, text in runs:
            painter.setFont(self._font_for_key(key))
            painter.setPen(self._color_for_key(key))
            painter.drawText(column * cell_width, baseline, text)

    def paintEvent(self, event):  # noqa: N802
        trace = _trace_enabled()
        paint_started = time.perf_counter() if trace else 0.0

        painter = QPainter(self.viewport())
        painter.setFont(self._font)

        width = self.viewport().width()
        height = self.viewport().height()

        # 背景
        painter.fillRect(QRect(0, 0, width, height), QColor(self._theme["background"]))

        cell_width = self._cell.width()
        cell_height = self._cell.height()

        columns = self.emulator.columns
        baseline = cell_height - QFontMetricsF(self._font).descent() - 1

        # 视口 = 虚拟轴上的一个窗口；视口第 view_row 行就是第 first + view_row 虚拟行
        total = self._total_lines()
        first = self._first_view_row()
        view_rows = self._viewport_rows()

        visible = [
            (view_row, first + view_row)
            for view_row in range(view_rows)
            if first + view_row < total
        ]

        selection = self._selection_range()

        # 底色层与文本层：只遍历"实际写入过的单元格"，并按同一样式合并成片段绘制
        for view_row, virtual in visible:
            top = view_row * cell_height
            line = self._raw_line(virtual)

            if not line:
                continue

            self._paint_line(
                painter,
                line,
                top=top,
                baseline=top + baseline,
                cell_width=cell_width,
                cell_height=cell_height,
            )

        painter.setFont(self._font)

        # 选区（选区记的是虚拟行号，滚动时跟着内容走）
        if selection:
            (start_row, start_column), (end_row, end_column) = selection

            for virtual in range(start_row, end_row + 1):
                view_row = virtual - first

                if not (0 <= view_row < view_rows):
                    continue

                first_column = start_column if virtual == start_row else 0
                last_column = end_column if virtual == end_row else columns - 1

                if last_column < first_column:
                    continue

                painter.fillRect(
                    QRect(
                        first_column * cell_width,
                        view_row * cell_height,
                        (last_column - first_column + 1) * cell_width,
                        cell_height,
                    ),
                    QColor(self._theme["selection"]),
                )

        painter.setFont(self._font)

        # 光标（只在贴底时显示）
        if self._offset == 0 and self.hasFocus() and self.emulator.cursor_visible:
            cursor = self.emulator.cursor
            cursor_view_row = self._view_row_for_screen_row(cursor.y)

            if cursor_view_row >= 0:
                cursor_top = cursor_view_row * cell_height

                painter.fillRect(
                    QRect(cursor.x * cell_width, cursor_top, cell_width, cell_height),
                    QColor(self._theme["cursor"]),
                )

                cell = self._cell_at_virtual(len(self._history_top) + cursor.y, cursor.x)

                if cell is not None and cell[0] != " ":
                    painter.setPen(QColor(self._theme["background"]))
                    painter.setFont(self._styled_font(cell[1]))
                    painter.drawText(cursor.x * cell_width, cursor_top + baseline, cell[0])

        painter.end()

        if trace:
            elapsed = time.perf_counter() - paint_started

            if elapsed > 0.05:
                trace_ui(f"paint {view_rows} 行：{elapsed * 1000:.0f}ms")

    def _styled_font(self, style: CellStyle) -> QFont:
        font = QFont(self._font)
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italics))
        font.setUnderline(bool(style.underscore))
        font.setStrikeOut(bool(style.strikethrough))

        return font

    # ------------------------------------------------------------------
    # 选区
    # ------------------------------------------------------------------

    def _selection_range(self):
        if self._anchor is None or self._cursor_cell is None:
            return None

        first, second = sorted((self._anchor, self._cursor_cell))

        return first, second

    def _cell_from_point(self, point: QPoint) -> tuple[int, int]:
        """鼠标位置 → 虚拟行号 + 列（选区跟着内容走，滚动也不会错位）。"""

        column = max(0, min(self.emulator.columns - 1, point.x() // self._cell.width()))
        view_row = max(0, min(self._viewport_rows() - 1, point.y() // self._cell.height()))
        total = self._total_lines()
        virtual = min(max(0, total - 1), self._first_view_row() + view_row) if total else 0

        return virtual, column

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._anchor = self._cell_from_point(event.position().toPoint())
            self._cursor_cell = self._anchor
            self._selecting = True
            self.viewport().update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._selecting:
            self._cursor_cell = self._cell_from_point(event.position().toPoint())
            self.viewport().update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._selecting = False

        # 选中即复制（PuTTY 风格，可在菜单里关掉）：把远端内容带到 Windows
        if self.select_to_copy:
            text = self.selected_text()

            if text:
                QGuiApplication.clipboard().setText(text)
                self.copied.emit(text)

    def selected_text(self) -> str:
        selection = self._selection_range()

        if selection is None:
            return ""

        (start_row, start_column), (end_row, end_column) = selection
        lines = []

        for virtual in range(start_row, end_row + 1):
            text = self._line_text(virtual)

            if virtual == start_row and virtual == end_row:
                lines.append(text[start_column : end_column + 1])
            elif virtual == start_row:
                lines.append(text[start_column:])
            elif virtual == end_row:
                lines.append(text[: end_column + 1])
            else:
                lines.append(text)

        return "\n".join(lines).rstrip()

    def copy(self) -> None:
        text = self.selected_text()

        if text:
            QGuiApplication.clipboard().setText(text)
            self.copied.emit(text)

    def cut(self) -> None:
        """剪切：复制选中内容到 Windows 剪贴板，并清掉本机的高亮选区。

        远端屏幕内容不受影响（终端是远端渲染的，本地删不掉），
        所以"剪切"在这里等于"复制 + 取消选中"，语义与终端习惯一致。
        """

        text = self.selected_text()

        if not text:
            return

        QGuiApplication.clipboard().setText(text)
        self.clear_selection()
        self.copied.emit(text)

    def clear_selection(self) -> None:
        self._anchor = None
        self._cursor_cell = None
        self._selecting = False
        self.viewport().update()

    def output_text(self, include_history: bool = True) -> str:
        """把终端输出取成纯文本：整条虚拟轴（历史 + 当前屏幕 + 底部历史）。"""

        start = 0 if include_history else len(self._history_top)
        lines = [self._line_text(virtual) for virtual in range(start, self._total_lines())]

        # 去掉尾部空行
        while lines and not lines[-1]:
            lines.pop()

        return "\n".join(lines)

    def copy_all_output(self, include_history: bool = True) -> str:
        """复制全部输出到 Windows 剪贴板（Linux→Windows）。"""

        text = self.output_text(include_history=include_history)

        if text:
            QGuiApplication.clipboard().setText(text)
            self.copied.emit(text)

        return text

    def save_output(self, path: Path, include_history: bool = True) -> int:
        """把输出保存成文件，返回写入字节数。"""

        text = self.output_text(include_history=include_history)
        payload = text + "\n"
        Path(path).write_text(payload, encoding="utf-8")

        return len(payload.encode("utf-8"))

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()

        if text:
            self.data_to_send.emit(text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8"))

    # ------------------------------------------------------------------
    # 键盘
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        modifiers = event.modifiers()

        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)

        # 复制 / 粘贴
        if control and shift and key == Qt.Key.Key_C:
            self.copy()
            return

        if control and shift and key == Qt.Key.Key_V:
            self.paste()
            return

        if control and shift and key == Qt.Key.Key_A:
            self._anchor = (0, 0)
            self._cursor_cell = (self.emulator.lines - 1, self.emulator.columns - 1)
            self.viewport().update()
            return

        # 字体缩放
        if control and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom(1)
            return

        if control and key == Qt.Key.Key_Minus:
            self._zoom(-1)
            return

        if control and key == Qt.Key.Key_0:
            self.apply_font(self._font.family(), 12)
            return

        # 有选中内容时 Ctrl+C 走复制，否则当作中断发给远端
        if control and not shift and key == Qt.Key.Key_C and self.selected_text():
            self.copy()
            return

        text = self._encode_key(event, control, shift, alt)

        if text:
            self._offset = 0
            self._sync_scrollbar()
            self.data_to_send.emit(text)

    def _encode_key(self, event, control: bool, shift: bool, alt: bool) -> bytes:
        key = event.key()
        name = Qt.Key(key).name

        if control and 0x41 <= key <= 0x5A:
            character = chr(key)
            encoded = ctrl_byte(character)

            if encoded:
                payload = encoded

                if alt:
                    payload = "\x1b" + payload

                return payload.encode("utf-8")

        if control and key == Qt.Key.Key_Space:
            return b"\x00"

        sequence = None

        if name in KEY_SEQUENCES:
            sequence = KEY_SEQUENCES[name]

            # 应用光标键模式（vim / less 等会打开 DECCKM）：方向键改发 SS3
            if self.emulator.application_cursor_keys and name in ("Up", "Down", "Right", "Left", "Home", "End"):
                sequence = "\x1bO" + sequence[-1]
        elif key == Qt.Key.Key_Backspace:
            sequence = "\x7f"
        elif key == Qt.Key.Key_Backtab:
            sequence = "\x1b[Z"

        if sequence is not None:
            payload = sequence

            if alt:
                payload = "\x1b" + payload

            return payload.encode("utf-8")

        text = event.text()

        if not text:
            return b""

        if control:
            encoded = ctrl_byte(text)

            if encoded:
                text = encoded
            else:
                return b""

        if alt:
            text = "\x1b" + text

        return text.encode("utf-8")
