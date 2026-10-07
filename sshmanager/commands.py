"""命令收藏与历史：命令库、历史记录，以及条目级的复制/剪切/粘贴/撤销。

- **收藏（snippets）**：按主机或全局保存常用命令，可分组、加备注，记录使用次数。
- **历史**：自动记录每条发送过的命令（连续重复只留一条），按主机区分。
- **条目级剪贴板/撤销**：命令库里的条目可以复制/剪切/粘贴到某个分组，
  删除、新建、粘贴都能撤销——撤销栈由 :class:`CommandStore` 自己维护。

存储为 ``<数据目录>/commands.json``，与主机/凭据分离，方便单独备份或删除。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODE_COPY = "copy"
MODE_CUT = "cut"

HISTORY_LIMIT = 300


@dataclass
class CommandSnippet:
    """一条收藏的命令。``host_id`` 为空表示全局可用。"""

    text: str
    group: str = ""
    note: str = ""
    host_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    use_count: int = 0
    last_used: float = 0.0

    @property
    def display(self) -> str:
        text = self.text.strip().splitlines()
        first = text[0] if text else ""

        if len(text) > 1:
            return f"{first} …（共 {len(text)} 行）"

        return first

    def copy(self, preserve_id: bool = False) -> "CommandSnippet":
        """复制一份；``preserve_id=True`` 用于剪切板（粘贴时还要按 id 找回原条目）。"""

        fresh = CommandSnippet(
            text=self.text,
            group=self.group,
            note=self.note,
            host_id=self.host_id,
        )

        if preserve_id:
            fresh.id = self.id

        fresh.use_count = self.use_count

        return fresh


@dataclass
class HistoryEntry:
    text: str
    host_id: str = ""
    time: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        lines = self.text.strip().splitlines()

        return lines[0] if lines else ""


@dataclass
class SnippetEdit:
    """一次可撤销的条目操作。"""

    label: str
    #: 撤销时要删掉的条目 id
    added: list[str] = field(default_factory=list)
    #: 撤销时要恢复的条目（原先被剪切/删除掉的）
    removed: list[CommandSnippet] = field(default_factory=list)
    #: 撤销时要还原的字段改动：id → 旧值字典
    changed: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed and not self.changed


@dataclass
class SnippetClipboard:
    """命令条目的剪贴板（复制 / 剪切）。"""

    mode: str = MODE_COPY
    items: list[CommandSnippet] = field(default_factory=list)
    target_group: str = ""

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def mode_label(self) -> str:
        return "复制" if self.mode == MODE_COPY else "剪切"

    def set(self, mode: str, items: list[CommandSnippet]) -> None:
        self.mode = mode if mode in (MODE_COPY, MODE_CUT) else MODE_COPY
        self.items = [item.copy(preserve_id=True) for item in items]

    def clear(self) -> None:
        self.items = []

    def summary(self) -> str:
        if self.empty:
            return "剪贴板为空"

        groups = sorted({item.group or "未分组" for item in self.items})

        return f"{self.mode_label} {len(self.items)} 条（{'、'.join(groups)}）"


class CommandStore:
    """命令库 + 历史的持久化与操作。"""

    def __init__(self, path: Path | str | None = None, undo_limit: int = 30) -> None:
        self.path = Path(path) if path is not None else self._default_path()
        self.snippets: list[CommandSnippet] = []
        self.history: list[HistoryEntry] = []
        self.undo_limit = max(1, int(undo_limit))
        self._undo: list[SnippetEdit] = []
        self.load()

    # ------------------------------------------------------------------

    @staticmethod
    def _default_path() -> Path:
        from . import settings

        return Path(settings.data_dir()) / "commands.json"

    def load(self) -> None:
        if not self.path.exists():
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return

        self.snippets = [CommandSnippet(**item) for item in payload.get("snippets", []) if item.get("text")]
        self.history = [HistoryEntry(**item) for item in payload.get("history", []) if item.get("text")]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "snippets": [asdict(item) for item in self.snippets],
            "history": [asdict(item) for item in self.history[:HISTORY_LIMIT]],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # 收藏
    # ------------------------------------------------------------------

    def groups(self) -> list[str]:
        return sorted({item.group for item in self.snippets if item.group})

    def snippets_for(self, host_id: str = "", group: str = "", keyword: str = "") -> list[CommandSnippet]:
        """返回适用于该主机的命令（全局 + 该主机），可按分组与关键词过滤。"""

        result = [
            item
            for item in self.snippets
            if item.host_id in ("", host_id)
        ]

        if group:
            result = [item for item in result if item.group == group]

        if keyword:
            needle = keyword.lower()
            result = [
                item
                for item in result
                if needle in item.text.lower() or needle in item.note.lower()
            ]

        return sorted(result, key=lambda item: (item.group, item.text))

    def get(self, snippet_id: str) -> CommandSnippet | None:
        return next((item for item in self.snippets if item.id == snippet_id), None)

    def add(
        self,
        text: str,
        group: str = "",
        note: str = "",
        host_id: str = "",
        record_undo: bool = True,
    ) -> CommandSnippet | None:
        """新增收藏；文本为空或与该主机已有条目重复时不新增。"""

        text = (text or "").strip()

        if not text:
            return None

        if any(item.text == text and item.host_id == host_id for item in self.snippets):
            return None

        snippet = CommandSnippet(text=text, group=group.strip(), note=note.strip(), host_id=host_id)
        self.snippets.append(snippet)

        if record_undo:
            self._push(SnippetEdit(label=f"新增命令「{snippet.display}」", added=[snippet.id]))

        self.save()

        return snippet

    def update(self, snippet_id: str, record_undo: bool = True, **fields) -> bool:
        snippet = self.get(snippet_id)

        if snippet is None:
            return False

        before = {key: getattr(snippet, key) for key in fields if hasattr(snippet, key)}

        if not before:
            return False

        for key, value in before.items():
            setattr(snippet, key, fields[key] if key not in ("text", "group", "note") else str(fields[key]).strip())

        if record_undo:
            self._push(SnippetEdit(label=f"编辑命令「{snippet.display}」", changed=[(snippet_id, before)]))

        self.save()

        return True

    def remove(self, snippet_ids: list[str], record_undo: bool = True, label: str = "删除命令") -> int:
        """删除若干条目，返回删除数量。"""

        targets = [item for item in self.snippets if item.id in set(snippet_ids)]

        if not targets:
            return 0

        self.snippets = [item for item in self.snippets if item.id not in set(snippet_ids)]

        if record_undo:
            self._push(SnippetEdit(label=f"{label} {len(targets)} 条", removed=targets))

        self.save()

        return len(targets)

    def mark_used(self, snippet_id: str) -> None:
        snippet = self.get(snippet_id)

        if snippet is None:
            return

        snippet.use_count += 1
        snippet.last_used = time.time()
        self.save()

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------

    def record_history(self, text: str, host_id: str = "") -> HistoryEntry | None:
        """记录一条执行过的命令；连续重复只保留一条。"""

        text = (text or "").strip()

        if not text:
            return None

        if self.history and self.history[0].text == text and self.history[0].host_id == host_id:
            self.history[0].time = time.time()
            self.save()

            return self.history[0]

        entry = HistoryEntry(text=text, host_id=host_id)
        self.history.insert(0, entry)
        del self.history[HISTORY_LIMIT:]
        self.save()

        return entry

    def history_for(self, host_id: str = "", keyword: str = "") -> list[HistoryEntry]:
        result = [item for item in self.history if not host_id or item.host_id in ("", host_id)]

        if keyword:
            needle = keyword.lower()
            result = [item for item in result if needle in item.text.lower()]

        return result

    def clear_history(self, host_id: str = "") -> int:
        before = len(self.history)

        if host_id:
            self.history = [item for item in self.history if item.host_id != host_id]
        else:
            self.history = []

        self.save()

        return before - len(self.history)

    # ------------------------------------------------------------------
    # 条目剪贴板：复制 / 剪切 / 粘贴
    # ------------------------------------------------------------------

    def paste(self, clipboard: SnippetClipboard, group: str = "") -> int:
        """把剪贴板里的条目粘贴到 ``group`` 分组。

        复制 → 生成新条目（不冲突地追加）；剪切 → 移动分组，原条目被删除（可撤销）。
        """

        if clipboard.empty:
            return 0

        if clipboard.mode == MODE_CUT:
            moved_from = []
            edited = []

            for item in clipboard.items:
                snippet = self.get(item.id)

                if snippet is None:
                    continue

                before = {"group": snippet.group}
                snippet.group = group
                edited.append((snippet.id, before))
                moved_from.append(snippet)

            if not edited:
                return 0

            self._push(
                SnippetEdit(
                    label=f"剪切 {len(edited)} 条到「{group or '未分组'}」",
                    changed=edited,
                )
            )
            self.save()

            return len(edited)

        added = []

        for item in clipboard.items:
            # 同一分组里已有同内容的条目就跳过；换一个分组则允许并存
            if any(snippet.text == item.text and snippet.group == group for snippet in self.snippets):
                continue

            fresh = item.copy()
            fresh.group = group
            self.snippets.append(fresh)
            added.append(fresh.id)

        if added:
            self._push(SnippetEdit(label=f"粘贴 {len(added)} 条到「{group or '未分组'}」", added=added))
            self.save()

        return len(added)

    # ------------------------------------------------------------------
    # 撤销
    # ------------------------------------------------------------------

    def _push(self, edit: SnippetEdit) -> None:
        if edit.empty:
            return

        self._undo.append(edit)
        del self._undo[: max(0, len(self._undo) - self.undo_limit)]

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def last_undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    def undo(self) -> str:
        """撤销上一次条目操作，返回说明文字。"""

        if not self._undo:
            return ""

        edit = self._undo.pop()

        # 先删掉这次新增的
        if edit.added:
            ids = set(edit.added)
            self.snippets = [item for item in self.snippets if item.id not in ids]

        # 再恢复被删掉的
        for snippet in edit.removed:
            if self.get(snippet.id) is None:
                self.snippets.append(snippet)

        # 最后还原字段
        for snippet_id, before in edit.changed:
            snippet = self.get(snippet_id)

            if snippet is None:
                continue

            for key, value in before.items():
                setattr(snippet, key, value)

        self.save()

        return edit.label


#: 进程内共享的命令库（界面各处都通过 :func:`get_store` 取，避免重复读写文件）
_STORE: CommandStore | None = None


def get_store(path: Path | str | None = None) -> CommandStore:
    """取命令库单例；传入 path 时另建一个（测试用）。"""

    global _STORE

    if path is not None:
        return CommandStore(path)

    if _STORE is None:
        _STORE = CommandStore()

    return _STORE


def reset_store() -> None:
    """清掉单例（测试用）。"""

    global _STORE
    _STORE = None
