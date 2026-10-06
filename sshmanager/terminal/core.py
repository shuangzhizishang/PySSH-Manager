"""终端仿真核心：pyte 封装 + 键盘输入编码。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import pyte


# ----------------------------------------------------------------------
# 键盘 -> 终端字节序列
# ----------------------------------------------------------------------

#: 普通按键（无修饰键）
KEY_SEQUENCES = {
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "Insert": "\x1b[2~",
    "Delete": "\x1b[3~",
    "PageUp": "\x1b[5~",
    "PageDown": "\x1b[6~",
    "F1": "\x1bOP",
    "F2": "\x1bOQ",
    "F3": "\x1bOR",
    "F4": "\x1bOS",
    "F5": "\x1b[15~",
    "F6": "\x1b[17~",
    "F7": "\x1b[18~",
    "F8": "\x1b[19~",
    "F9": "\x1b[20~",
    "F10": "\x1b[21~",
    "F11": "\x1b[23~",
    "F12": "\x1b[24~",
    "Backspace": "\x7f",
    "Tab": "\t",
    "Return": "\r",
    "Enter": "\r",
    "Escape": "\x1b",
    "Space": " ",
}

#: Ctrl+字母 -> 控制字符（a=0x01 ... z=0x1a，另有 @ [ \ ] ^ _）
def ctrl_byte(character: str) -> str | None:
    if not character:
        return None

    code = ord(character.upper())

    if 0x40 <= code <= 0x5F:
        return chr(code - 0x40)

    if character == " ":
        return "\x00"

    return None


# ----------------------------------------------------------------------
# 颜色
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CellStyle:
    """一个字符单元的显示属性。"""

    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False


class Emulator:
    """把 SSH 字节流喂给 pyte，并暴露屏幕内容。"""

    def __init__(self, columns: int = 120, lines: int = 32) -> None:
        self._columns = max(2, int(columns))
        self._lines = max(2, int(lines))

        self.screen = pyte.HistoryScreen(
            self._columns,
            self._lines,
            history=2000,
            ratio=0.5,
        )
        self.screen.set_mode(pyte.modes.LNM)

        self._stream = pyte.ByteStream(self.screen)

    # ------------------------------------------------------------------
    # 基本信息
    # ------------------------------------------------------------------

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def lines(self) -> int:
        return self._lines

    @property
    def title(self) -> str:
        return getattr(self.screen, "title", "") or ""

    @property
    def cursor(self):
        return self.screen.cursor

    @property
    def cursor_visible(self) -> bool:
        """DECTCEM 控制的光标可见性（pyte 里叫 hidden）。"""

        return not getattr(self.screen.cursor, "hidden", False)

    @property
    def application_cursor_keys(self) -> bool:
        """DECCKM：应用模式下方向键要发 SS3 序列。"""

        return bool(self.screen.mode & pyte.modes.DECCKM)

    # ------------------------------------------------------------------
    # 数据流
    # ------------------------------------------------------------------

    def feed(self, data: bytes | str) -> None:
        """喂入服务器输出。"""

        if not data:
            return

        self._stream.feed(data)

    def resize(self, columns: int, lines: int) -> bool:
        """调整终端尺寸，返回是否真的变化了。"""

        columns = max(2, int(columns))
        lines = max(2, int(lines))

        if (columns, lines) == (self._columns, self._lines):
            return False

        self._columns = columns
        self._lines = lines

        self.screen.resize(lines=lines, columns=columns)

        return True

    # ------------------------------------------------------------------
    # 读取屏幕
    # ------------------------------------------------------------------

    def visible_lines(self) -> list[str]:
        """返回当前屏幕每一行的纯文本（已去掉行尾空格）。"""

        buffer = self.screen.buffer
        result: list[str] = []

        for row in range(self._lines):
            line = buffer.get(row, {})
            text = "".join(line.get(col, pyte.screens.Char(" ")).data for col in range(self._columns))
            result.append(text.rstrip())

        return result

    def iter_cells(self) -> Iterator[tuple[int, int, str, CellStyle]]:
        """遍历屏幕上的非空单元，产出 (row, column, char, style)。"""

        buffer = self.screen.buffer

        for row in range(self._lines):
            line = buffer.get(row, {})

            for column in range(self._columns):
                char = line.get(column)

                if char is None:
                    continue

                if char.data == " " and char.bg == "default" and not char.reverse:
                    continue

                yield row, column, char.data, CellStyle(
                    fg=char.fg,
                    bg=char.bg,
                    bold=char.bold,
                    italics=char.italics,
                    underscore=char.underscore,
                    strikethrough=char.strikethrough,
                    reverse=char.reverse,
                )

    def plain_text(self) -> str:
        return "\n".join(self.visible_lines())
