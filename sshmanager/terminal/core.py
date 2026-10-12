"""终端仿真核心：pyte 封装 + 键盘输入编码。"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Iterator

import pyte

#: 回滚历史保留多少行
HISTORY_LINES = 2000


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


class Scrollback:
    """最简回滚缓冲：只保存滚出屏幕的行。

    保持与 pyte ``HistoryScreen.history`` 一样的访问方式（``top`` / ``bottom``），
    这样界面层不用改。
    """

    __slots__ = ("top", "bottom")

    def __init__(self, limit: int = 2000) -> None:
        self.top: deque = deque(maxlen=max(1, int(limit)))
        self.bottom: deque = deque()


class FastScreen(pyte.Screen):
    """普通 ``pyte.Screen`` + 自己记录回滚行。

    **为什么不用 pyte 的 HistoryScreen**：它会重写 ``__getattribute__``，给每个
    流事件都现造一个闭包，并在 ``after_event`` 里做额外处理；实测解析吞吐只有普通
    ``Screen`` 的四分之一（1.4 MB 文本要 34 秒），远端刷屏时界面直接卡死。
    这里改为只用普通 Screen，在整屏上滚时把离开屏幕的那一行收进有界队列，
    效果一致但快得多。
    """

    def __init__(self, columns: int, lines: int, history: int = 2000) -> None:
        self.history = Scrollback(history)
        super().__init__(columns, lines)

    def index(self) -> None:  # noqa: D102 - 见类文档
        top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)

        # 只有"整屏上滚"才计入回滚历史；局部滚动区域（如 vim 的分屏）不算
        if top == 0 and bottom == self.lines - 1 and self.cursor.y == bottom:
            self.history.top.append(self.buffer[0].copy())

        super().index()


class Emulator:
    """把 SSH 字节流喂给 pyte，并暴露屏幕内容。"""

    def __init__(self, columns: int = 120, lines: int = 32) -> None:
        self._columns = max(2, int(columns))
        self._lines = max(2, int(lines))

        self.screen = FastScreen(
            self._columns,
            self._lines,
            history=HISTORY_LINES,
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
        """喂入服务器输出。

        ``str`` 会被编码成 UTF-8 字节再解析，保持与 ``ByteStream`` 一致的解码语义。
        """

        if not data:
            return

        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")

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
