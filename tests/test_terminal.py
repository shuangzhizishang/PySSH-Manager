"""终端仿真核心测试。"""

from __future__ import annotations

from sshmanager.terminal import Emulator, ctrl_byte


def test_plain_text_and_cursor():
    emulator = Emulator(columns=20, lines=3)
    emulator.feed(b"hello")

    assert emulator.visible_lines()[0] == "hello"
    assert emulator.cursor.x == 5
    assert emulator.cursor.y == 0


def test_carriage_return_overwrite():
    emulator = Emulator(columns=20, lines=3)
    emulator.feed(b"abcdef\r\n")
    emulator.feed(b"\x1b[1;1HXY")

    assert emulator.visible_lines()[0] == "XYcdef"


def test_sgr_colors_recorded():
    emulator = Emulator(columns=20, lines=3)
    emulator.feed(b"\x1b[31mred\x1b[0m plain")

    cells = {(row, column): (char, style) for row, column, char, style in emulator.iter_cells()}

    assert cells[(0, 0)][1].fg == "red"
    assert cells[(0, 0)][0] == "r"
    assert cells[(0, 4)][1].fg == "default"


def test_utf8_bytes_decoded():
    emulator = Emulator(columns=30, lines=3)
    emulator.feed("中文测试".encode("utf-8"))

    assert emulator.visible_lines()[0] == "中文测试"


def test_resize_changes_grid():
    emulator = Emulator(columns=20, lines=3)

    assert emulator.resize(40, 10) is True
    assert (emulator.columns, emulator.lines) == (40, 10)
    assert emulator.resize(40, 10) is False


def test_scroll_keeps_cursor_area():
    emulator = Emulator(columns=20, lines=3)

    for index in range(5):
        emulator.feed(f"line{index}\r\n".encode())

    lines = emulator.visible_lines()

    assert lines[-1] == "" or lines[-1].startswith("line")
    assert any("line4" in line for line in lines)


def test_ctrl_bytes():
    assert ctrl_byte("c") == "\x03"
    assert ctrl_byte("C") == "\x03"
    assert ctrl_byte("[") == "\x1b"
    assert ctrl_byte(" ") == "\x00"
    assert ctrl_byte("1") is None
