"""文件剪贴板、撤销栈与本地文件操作的单元测试（不需要网络）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sshmanager.fileops import (
    MODE_COPY,
    MODE_CUT,
    SIDE_LOCAL,
    SIDE_REMOTE,
    ClipboardItem,
    FileClipboard,
    UndoEntry,
    UndoStack,
    copy_local,
    local_trash_root,
    move_local,
    plan_paste,
    remove_local,
    trash_local,
    unique_local_path,
)


# ----------------------------------------------------------------------
# 命名冲突
# ----------------------------------------------------------------------

def test_unique_local_path_keeps_original_when_free(tmp_path):
    assert unique_local_path(tmp_path, "a.txt") == tmp_path / "a.txt"


def test_unique_local_path_appends_index(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a (2).txt").write_text("x", encoding="utf-8")

    assert unique_local_path(tmp_path, "a.txt") == tmp_path / "a (3).txt"


# ----------------------------------------------------------------------
# 本地复制 / 移动 / 回收站
# ----------------------------------------------------------------------

def test_copy_local_file(tmp_path):
    source = tmp_path / "src.txt"
    source.write_text("hello", encoding="utf-8")

    target = copy_local(source, tmp_path / "out" / "dst.txt")

    assert target.read_text(encoding="utf-8") == "hello"
    assert source.exists(), "复制不应影响源文件"


def test_copy_local_directory_recursive(tmp_path):
    source = tmp_path / "tree"
    (source / "sub").mkdir(parents=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    (source / "sub" / "b.txt").write_text("b", encoding="utf-8")

    target = copy_local(source, tmp_path / "copy")

    assert (target / "a.txt").read_text(encoding="utf-8") == "a"
    assert (target / "sub" / "b.txt").read_text(encoding="utf-8") == "b"


def test_copy_local_avoids_overwrite(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("new", encoding="utf-8")
    existing = tmp_path / "out"
    existing.mkdir()
    (existing / "a.txt").write_text("old", encoding="utf-8")

    target = copy_local(source, existing / "a.txt")

    assert target.name == "a (2).txt"
    assert (existing / "a.txt").read_text(encoding="utf-8") == "old"


def test_move_local(tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("x", encoding="utf-8")
    target_dir = tmp_path / "d"
    target_dir.mkdir()

    target = move_local(source, target_dir / "a.txt")

    assert target.exists() and not source.exists()


def test_trash_and_restore_local(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    source = tmp_path / "gone.txt"
    source.write_text("payload", encoding="utf-8")

    trashed = trash_local(source)

    assert not source.exists()
    assert trashed.exists()
    assert trashed.read_text(encoding="utf-8") == "payload"
    assert str(local_trash_root()) in str(trashed)

    # 撤销 = 移回原位
    move_local(trashed, source)
    assert source.read_text(encoding="utf-8") == "payload"


def test_remove_local_file_and_dir(tmp_path):
    file_path = tmp_path / "f.txt"
    file_path.write_text("x", encoding="utf-8")
    directory = tmp_path / "d"
    (directory / "inner").mkdir(parents=True)

    remove_local(file_path)
    remove_local(directory)

    assert not file_path.exists() and not directory.exists()


# ----------------------------------------------------------------------
# 剪贴板
# ----------------------------------------------------------------------

def test_clipboard_set_and_summary():
    clipboard = FileClipboard()
    assert clipboard.empty and clipboard.summary() == "剪贴板为空"

    clipboard.set(MODE_COPY, [ClipboardItem(SIDE_LOCAL, "C:/a.txt", "a.txt")])
    assert not clipboard.empty
    assert "复制 1 项" in clipboard.summary()
    assert "本地" in clipboard.summary()

    clipboard.set(MODE_CUT, [ClipboardItem(SIDE_REMOTE, "/root/b", "b", is_dir=True)])
    assert "剪切 1 项" in clipboard.summary()
    assert "远端" in clipboard.summary()

    clipboard.set("nonsense", [])
    assert clipboard.mode == MODE_COPY, "未知模式应回退为复制"


def test_clipboard_mixed_sides_summary():
    clipboard = FileClipboard()
    clipboard.set(
        MODE_COPY,
        [
            ClipboardItem(SIDE_LOCAL, "C:/a", "a"),
            ClipboardItem(SIDE_REMOTE, "/root/b", "b"),
        ],
    )

    assert "本地+远端" in clipboard.summary()


def test_plan_paste_directions():
    local = [ClipboardItem(SIDE_LOCAL, "C:/a.txt", "a.txt")]
    remote = [ClipboardItem(SIDE_REMOTE, "/root/b.txt", "b.txt")]

    assert "上传" in plan_paste(local, SIDE_REMOTE)[0]
    assert "下载" in plan_paste(remote, SIDE_LOCAL)[0]
    assert "本地复制" in plan_paste(local, SIDE_LOCAL)[0]
    assert "远端复制" in plan_paste(remote, SIDE_REMOTE)[0]


# ----------------------------------------------------------------------
# 撤销栈
# ----------------------------------------------------------------------

def test_undo_stack_push_pop_and_limit():
    stack = UndoStack(limit=2)

    assert not stack.can_undo

    for index in range(3):
        stack.push(UndoEntry(label=f"op{index}", created=[(SIDE_LOCAL, f"/x{index}")]))

    assert stack.depth == 2, "超出上限时丢弃最旧的记录"
    assert stack.last_label == "op2"

    assert stack.pop().label == "op2"
    assert stack.pop().label == "op1"
    assert stack.pop() is None


def test_undo_stack_ignores_empty_entries():
    stack = UndoStack()
    stack.push(UndoEntry(label="nothing"))

    assert not stack.can_undo


def test_undo_entry_holds_both_created_and_moved():
    entry = UndoEntry(
        label="剪切 1 项",
        created=[(SIDE_REMOTE, "/root/dst.txt")],
        moved=[(SIDE_LOCAL, "C:/src.txt", "C:/trash/src.txt")],
    )

    assert entry.created[0][1].endswith("dst.txt")
    assert entry.moved[0][1] == "C:/src.txt"


def test_undo_stack_clear():
    stack = UndoStack()
    stack.push(UndoEntry(label="op", created=[(SIDE_LOCAL, "/a")]))
    stack.clear()

    assert not stack.can_undo
