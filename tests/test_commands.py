"""命令库单元测试：收藏、历史、条目剪贴板与撤销（不需要网络）。"""

from __future__ import annotations

import json

from sshmanager.commands import (
    HISTORY_LIMIT,
    MODE_COPY,
    MODE_CUT,
    CommandStore,
    SnippetClipboard,
)


def make_store(tmp_path):
    return CommandStore(tmp_path / "commands.json")


# ----------------------------------------------------------------------
# 收藏
# ----------------------------------------------------------------------

def test_add_and_duplicate(tmp_path):
    store = make_store(tmp_path)

    first = store.add("ls -al", group="常用")
    assert first is not None
    assert len(store.snippets) == 1

    # 同主机同内容不再添加
    assert store.add("ls -al", group="常用") is None
    assert len(store.snippets) == 1

    # 空内容不添加
    assert store.add("   ") is None
    assert len(store.snippets) == 1


def test_snippets_for_filters_by_host_group_keyword(tmp_path):
    store = make_store(tmp_path)
    store.add("df -h", group="磁盘", host_id="h1")
    store.add("free -m", group="内存", host_id="h1")
    store.add("uptime", group="", host_id="")
    store.add("other-host-cmd", host_id="h2")

    mine = {item.text for item in store.snippets_for("h1")}
    assert mine == {"df -h", "free -m", "uptime"}, "应包含全局与本机条目，不含其他主机"

    assert {item.text for item in store.snippets_for("h1", group="磁盘")} == {"df -h"}
    assert {item.text for item in store.snippets_for("h1", keyword="free")} == {"free -m"}
    assert {item.text for item in store.snippets_for("h1", keyword="无此内容")} == set()


def test_update_and_groups(tmp_path):
    store = make_store(tmp_path)
    snippet = store.add("ls", group="a", note="旧备注")

    assert store.update(snippet.id, note="新备注", group="b")
    assert store.get(snippet.id).note == "新备注"
    assert store.groups() == ["b"]

    assert not store.update("不存在的id", note="x")


def test_mark_used(tmp_path):
    store = make_store(tmp_path)
    snippet = store.add("ls")

    store.mark_used(snippet.id)

    assert store.get(snippet.id).use_count == 1
    assert store.get(snippet.id).last_used > 0


def test_remove_and_undo(tmp_path):
    store = make_store(tmp_path)
    first = store.add("ls")
    second = store.add("pwd")

    assert store.remove([first.id]) == 1
    assert len(store.snippets) == 1

    label = store.undo()
    assert "删除" in label
    assert {item.text for item in store.snippets} == {"ls", "pwd"}


def test_undo_add(tmp_path):
    store = make_store(tmp_path)
    store.add("ls")

    store.undo()

    assert store.snippets == []


def test_undo_update(tmp_path):
    store = make_store(tmp_path)
    snippet = store.add("ls", group="a")

    store.update(snippet.id, group="b")
    store.undo()

    assert store.get(snippet.id).group == "a"


# ----------------------------------------------------------------------
# 历史
# ----------------------------------------------------------------------

def test_history_records_and_dedupes_consecutive(tmp_path):
    store = make_store(tmp_path)

    store.record_history("ls", "h1")
    store.record_history("ls", "h1")
    store.record_history("pwd", "h1")
    store.record_history("ls", "h1")

    assert [entry.text for entry in store.history] == ["ls", "pwd", "ls"]


def test_history_limit_and_filter(tmp_path):
    store = make_store(tmp_path)

    for index in range(HISTORY_LIMIT + 20):
        store.record_history(f"cmd-{index}", "h1")

    assert len(store.history) == HISTORY_LIMIT
    assert store.history[0].text == f"cmd-{HISTORY_LIMIT + 19}"

    store.record_history("other", "h2")

    assert [entry.text for entry in store.history_for("h2")][0] == "other"
    assert "other" not in {entry.text for entry in store.history_for("h1")}
    assert store.history_for("h1", keyword="cmd-5")


def test_clear_history_for_one_host(tmp_path):
    store = make_store(tmp_path)
    store.record_history("a", "h1")
    store.record_history("b", "h2")

    assert store.clear_history("h1") == 1
    assert {entry.text for entry in store.history} == {"b"}

    assert store.clear_history() == 1
    assert store.history == []


# ----------------------------------------------------------------------
# 条目剪贴板：复制 / 剪切 / 粘贴
# ----------------------------------------------------------------------

def test_clipboard_summary_and_mode_fallback(tmp_path):
    store = make_store(tmp_path)
    clipboard = SnippetClipboard()
    assert clipboard.empty and clipboard.summary() == "剪贴板为空"

    snippet = store.add("ls", group="a")
    clipboard.set(MODE_COPY, [snippet])

    assert not clipboard.empty
    assert "复制 1 条" in clipboard.summary()
    assert "a" in clipboard.summary()

    clipboard.set("乱写", [snippet])
    assert clipboard.mode == MODE_COPY


def test_paste_copy_duplicates_into_group(tmp_path):
    store = make_store(tmp_path)
    snippet = store.add("ls", group="a")

    clipboard = SnippetClipboard()
    clipboard.set(MODE_COPY, [snippet])

    assert store.paste(clipboard, "b") == 1
    assert len(store.snippets) == 2
    assert store.get(snippet.id).group == "a", "复制不改动原条目"

    pasted = [item for item in store.snippets if item.id != snippet.id][0]
    assert pasted.group == "b"

    store.undo()
    assert len(store.snippets) == 1, "撤销粘贴应删掉新条目"


def test_paste_copy_dedupes_within_same_group(tmp_path):
    store = make_store(tmp_path)
    first = store.add("ls", group="a")
    second = store.add("pwd", group="a")

    clipboard = SnippetClipboard()
    clipboard.set(MODE_COPY, [first, second])

    # 目标分组里已有同内容 → 跳过，不重复添加
    assert store.paste(clipboard, "a") == 0
    assert len(store.snippets) == 2

    # 换到别的分组则允许并存（同一条命令可以在多个分组里）
    assert store.paste(clipboard, "b") == 2
    assert len(store.snippets) == 4


def test_paste_cut_moves_group_and_undo_restores(tmp_path):
    store = make_store(tmp_path)
    snippet = store.add("ls", group="a")

    clipboard = SnippetClipboard()
    clipboard.set(MODE_CUT, [snippet])

    assert store.paste(clipboard, "b") == 1
    assert store.get(snippet.id).group == "b"
    assert len(store.snippets) == 1

    store.undo()
    assert store.get(snippet.id).group == "a"


def test_paste_empty_clipboard(tmp_path):
    store = make_store(tmp_path)

    assert store.paste(SnippetClipboard(), "x") == 0


def test_undo_stack_is_bounded(tmp_path):
    store = CommandStore(tmp_path / "commands.json", undo_limit=2)

    for index in range(4):
        store.add(f"cmd-{index}")

    assert store.can_undo

    store.undo()
    store.undo()

    assert not store.can_undo, "只保留最近 2 步"
    assert len(store.snippets) == 2


# ----------------------------------------------------------------------
# 持久化
# ----------------------------------------------------------------------

def test_persistence_round_trip(tmp_path):
    path = tmp_path / "commands.json"
    store = CommandStore(path)
    snippet = store.add("ls -al", group="常用", note="列目录", host_id="h1")
    store.record_history("ls -al", "h1")
    store.mark_used(snippet.id)

    reopened = CommandStore(path)

    assert [item.text for item in reopened.snippets] == ["ls -al"]
    assert reopened.snippets[0].group == "常用"
    assert reopened.snippets[0].note == "列目录"
    assert reopened.snippets[0].use_count == 1
    assert [entry.text for entry in reopened.history] == ["ls -al"]


def test_load_ignores_corrupted_file(tmp_path):
    path = tmp_path / "commands.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    store = CommandStore(path)

    assert store.snippets == [] and store.history == []


def test_save_writes_utf8_json(tmp_path):
    path = tmp_path / "commands.json"
    store = CommandStore(path)
    store.add("echo 中文", group="中文分组")

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["snippets"][0]["text"] == "echo 中文"
    assert payload["snippets"][0]["group"] == "中文分组"
