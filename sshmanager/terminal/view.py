"""Qt 终端控件：字符网格渲染 + 键盘输入编码 + 滚动缓冲。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QAbstractScrollArea

from .core import KEY_SEQUENCES, CellStyle, Emulator, ctrl_byte

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

        # 回滚：offset 表示视口相对「屏幕顶部」向上偏移的行数
        self._offset = 0

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
        self._update_size_hint()
        self.viewport().update()

    def apply_theme(self, theme: dict) -> None:
        self._theme.update(theme)
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

        self.emulator.feed(data)

        title = self.emulator.title

        if title != self._last_title:
            self._last_title = title
            self.title_changed.emit(title)

        self._sync_scrollbar()
        self.viewport().update()

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
    # 回滚历史直接用 pyte HistoryScreen 自带的 history，不再自己推断滚屏：
    # 早期版本靠逐行比对猜「滚了几行」，会在每次输出时误判并不断累加
    # offset，最终把画面整个推出视口（表现为终端一片空白）。
    # ------------------------------------------------------------------

    @property
    def _history_top(self):
        return self.emulator.screen.history.top

    def _max_offset(self) -> int:
        """最多能向上回看多少行。"""

        return len(self._history_top)

    def _sync_scrollbar(self) -> None:
        bar = self.verticalScrollBar()
        maximum = self._max_offset()

        self._offset = max(0, min(self._offset, maximum))

        bar.blockSignals(True)
        bar.setRange(0, maximum)
        bar.setPageStep(max(1, self.emulator.lines))
        bar.setValue(maximum - self._offset)
        bar.blockSignals(False)

    def _on_scroll(self, value: int) -> None:
        self._offset = self._max_offset() - value
        self.viewport().update()

    def _line_index_for_row(self, row: int) -> int:
        """屏幕行 row 在当前滚动位置下对应的「历史 + 屏幕」行号。"""

        return len(self._history_top) - self._offset + row

    def _history_line(self, index: int):
        top = self._history_top

        if 0 <= index < len(top):
            return top[index]

        bottom_offset = index - (len(top) + self.emulator.lines)

        if 0 <= bottom_offset < len(self.emulator.screen.history.bottom):
            return self.emulator.screen.history.bottom[bottom_offset]

        return None

    def _row_text(self, row: int) -> str:
        """屏幕行 row 在当前滚动位置下的纯文本。"""

        index = self._line_index_for_row(row)

        if index < 0 or index >= len(self._history_top):
            lines = self.emulator.visible_lines()
            screen_index = index - len(self._history_top)

            if 0 <= screen_index < len(lines):
                return lines[screen_index]

            line = self._history_line(index)

            if line is None:
                return ""

            return "".join(line[column].data for column in range(self.emulator.columns)).rstrip()

        line = self._history_top[index]

        if line is None:
            return ""

        return "".join(line[column].data for column in range(self.emulator.columns)).rstrip()

    def _cell_from_history(self, line, column: int):
        char = line.get(column)

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
        theme = self._theme

        if name in theme:
            return QColor(theme[name])

        if name.startswith("#") and len(name) in (4, 7):
            return QColor(name)

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
            return QColor(palette[name])

        if name.startswith("bright") and name[6:] in palette:
            return QColor(palette[name[6:]]).lighter(125)

        if name in ("default", ""):
            return QColor(theme["foreground"])

        return QColor(theme["foreground"])

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self.viewport())
        painter.setFont(self._font)

        width = self.viewport().width()
        height = self.viewport().height()

        # 背景
        painter.fillRect(QRect(0, 0, width, height), QColor(self._theme["background"]))

        cell_width = self._cell.width()
        cell_height = self._cell.height()

        columns = self.emulator.columns
        lines = self.emulator.lines
        baseline = self._cell.height() - QFontMetricsF(self._font).descent() - 1

        # 历史行不足一屏时，画面从底部开始排（与真实终端一致）
        history_rows = len(self._history_top)
        shift = max(0, self.viewport().height() // cell_height - (history_rows + lines))

        def row_y(row: int) -> int:
            return (row + history_rows - self._offset + shift) * cell_height

        selection = self._selection_range()

        # 底色层：铺每个单元的 ANSI 背景色（历史行没有属性，跳过）
        for row in range(lines):
            top = row_y(row)

            if top + cell_height < 0 or top > height:
                continue

            for column in range(columns):
                cell = self._cell_at(row, column)

                background = None

                if cell is not None:
                    style = cell[1]

                    if style.bg not in ("default", ""):
                        background = self._resolve_color(style.bg)

                    if style.reverse:
                        background = self._resolve_color(style.fg)

                if background is not None:
                    painter.fillRect(
                        QRect(column * cell_width, top, cell_width, cell_height),
                        background,
                    )

        # 选区
        if selection:
            (start_row, start_column), (end_row, end_column) = selection

            for row in range(start_row, end_row + 1):
                first = start_column if row == start_row else 0
                last = end_column if row == end_row else columns - 1

                if last < first:
                    continue

                painter.fillRect(
                    QRect(
                        first * cell_width,
                        row_y(row),
                        (last - first + 1) * cell_width,
                        cell_height,
                    ),
                    QColor(self._theme["selection"]),
                )

        # 文本层
        for row in range(lines):
            top = row_y(row)

            if top + cell_height < 0 or top > height:
                continue

            y = top + baseline

            for column in range(columns):
                cell = self._cell_at(row, column)

                if cell is None:
                    continue

                char, style = cell

                if char == " " and style.bg in ("default", "") and not style.reverse:
                    continue

                # 回滚出来的历史行没有属性信息，用默认色
                if self._is_history_row(row):
                    color = QColor(self._theme["foreground"])
                    font = self._font
                else:
                    color = self._resolve_color(style.bg if style.reverse else style.fg)
                    font = self._styled_font(style)

                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(column * cell_width, y, char)

        painter.setFont(self._font)

        # 光标（只在贴底时显示）
        if self._offset == 0 and self.hasFocus() and self.emulator.cursor_visible:
            cursor = self.emulator.cursor
            cursor_top = row_y(cursor.y)

            if 0 <= cursor_top <= height - cell_height:
                painter.fillRect(
                    QRect(cursor.x * cell_width, cursor_top, cell_width, cell_height),
                    QColor(self._theme["cursor"]),
                )

                cell = self._cell_at(cursor.y, cursor.x)

                if cell is not None and cell[0] != " ":
                    painter.setPen(QColor(self._theme["background"]))
                    painter.setFont(self._styled_font(cell[1]))
                    painter.drawText(cursor.x * cell_width, cursor_top + baseline, cell[0])

        painter.end()

    def _is_history_row(self, row: int) -> bool:
        """该视口行是否来自回滚历史（而非当前屏幕）。"""

        return self._line_index_for_row(row) < len(self._history_top)

    def _cell_at(self, row: int, column: int) -> tuple[str, CellStyle] | None:
        """取视口坐标 (row, column) 处的字符与属性。"""

        if not (0 <= row < self.emulator.lines):
            return None

        index = self._line_index_for_row(row)

        if index < len(self._history_top):
            line = self._history_top[index] if index >= 0 else None

            if line is None:
                return None

            return self._cell_from_history(line, column)

        screen_row = index - len(self._history_top)

        if not (0 <= screen_row < self.emulator.lines):
            return None

        line = self.emulator.screen.buffer.get(screen_row)

        if line is None:
            return None

        char = line.get(column)

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
        column = max(0, min(self.emulator.columns - 1, point.x() // self._cell.width()))
        row = max(0, min(self.emulator.lines - 1, point.y() // self._cell.height()))

        return row, column

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

        for row in range(start_row, end_row + 1):
            text = self._row_text(row)

            if row == start_row and row == end_row:
                lines.append(text[start_column : end_column + 1])
            elif row == start_row:
                lines.append(text[start_column:])
            elif row == end_row:
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
        """把终端输出取成纯文本：回滚区（可选）+ 当前屏幕。"""

        lines: list[str] = []

        if include_history:
            history = self._history_top

            for index in range(len(history)):
                line = self._history_line(index)

                if line is None:
                    continue

                lines.append("".join(cell.data for cell in line).rstrip())

        for row in range(self.emulator.lines):
            lines.append(self._row_text(row).rstrip())

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
