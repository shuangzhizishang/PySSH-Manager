"""文件剪贴板与撤销：SFTP 面板的复制 / 剪切 / 粘贴 / 撤销。

设计要点：

- **剪贴板**只记录"来源侧 + 路径"，粘贴时按来源与目标侧自动决定动作：
  Windows→Linux 就是上传、Linux→Windows 就是下载、同侧则是本地/远端内部复制。
- **撤销**：每个操作产出一条 :class:`UndoEntry`，记录"新建了什么"与"从哪移到哪"。
  删除实现为**移动到回收站**（同侧），因此也能撤销；撤销时先按逆序移回、再删除新建项。
- 本地操作走 :mod:`sshmanager.fileworker` 的后台线程；远端操作沿用 SftpWorker。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

SIDE_LOCAL = "local"
SIDE_REMOTE = "remote"

MODE_COPY = "copy"
MODE_CUT = "cut"

#: 回收站目录名（本地放在应用数据目录，远端放在用户家目录）
TRASH_DIR_NAME = ".pyssh-trash"


# ----------------------------------------------------------------------
# 剪贴板
# ----------------------------------------------------------------------

@dataclass
class ClipboardItem:
    """剪贴板里的一项。``path`` 在本地是普通路径，在远端是 POSIX 路径。"""

    side: str
    path: str
    name: str
    is_dir: bool = False

    @property
    def side_label(self) -> str:
        return "Windows(本地)" if self.side == SIDE_LOCAL else "Linux(远端)"


@dataclass
class FileClipboard:
    """跨栏共享的文件剪贴板。"""

    mode: str = MODE_COPY
    items: list[ClipboardItem] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def mode_label(self) -> str:
        return "复制" if self.mode == MODE_COPY else "剪切"

    def set(self, mode: str, items: list[ClipboardItem]) -> None:
        self.mode = mode if mode in (MODE_COPY, MODE_CUT) else MODE_COPY
        self.items = list(items)

    def clear(self) -> None:
        self.items = []

    def summary(self) -> str:
        if self.empty:
            return "剪贴板为空"

        sides = {item.side for item in self.items}
        where = "本地" if sides == {SIDE_LOCAL} else "远端" if sides == {SIDE_REMOTE} else "本地+远端"

        return f"{self.mode_label} {len(self.items)} 项（来自{where}）"


# ----------------------------------------------------------------------
# 撤销
# ----------------------------------------------------------------------

@dataclass
class UndoEntry:
    """一次可撤销的操作。"""

    label: str
    #: 操作新建出来的东西：(side, path)，撤销时删除
    created: list[tuple[str, str]] = field(default_factory=list)
    #: 被移动的东西：(side, 原路径, 现路径)，撤销时移回
    moved: list[tuple[str, str, str]] = field(default_factory=list)

    def describe(self) -> str:
        return self.label


class UndoStack:
    """有界撤销栈（后进先出）。"""

    def __init__(self, limit: int = 20) -> None:
        self.limit = max(1, int(limit))
        self._entries: list[UndoEntry] = []

    # ------------------------------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._entries)

    @property
    def can_undo(self) -> bool:
        return bool(self._entries)

    @property
    def last_label(self) -> str:
        return self._entries[-1].label if self._entries else ""

    def push(self, entry: UndoEntry) -> None:
        if not entry.created and not entry.moved:
            return

        self._entries.append(entry)

        if len(self._entries) > self.limit:
            del self._entries[: len(self._entries) - self.limit]

    def pop(self) -> UndoEntry | None:
        return self._entries.pop() if self._entries else None

    def peek(self) -> UndoEntry | None:
        return self._entries[-1] if self._entries else None

    def clear(self) -> None:
        self._entries.clear()


# ----------------------------------------------------------------------
# 本地文件操作
# ----------------------------------------------------------------------

def unique_local_path(directory: Path, name: str) -> Path:
    """在目录里为 ``name`` 找一个不冲突的路径（``a.txt`` → ``a (2).txt``）。"""

    directory = Path(directory)
    candidate = directory / name

    if not candidate.exists():
        return candidate

    stem, suffix = Path(name).stem, Path(name).suffix
    index = 2

    while True:
        candidate = directory / f"{stem} ({index}){suffix}"

        if not candidate.exists():
            return candidate

        index += 1


def copy_local(source: Path, target: Path) -> Path:
    """复制本地文件/目录（递归），返回最终落点。"""

    source = Path(source)
    target = Path(target)

    if target.exists():
        target = unique_local_path(target.parent, target.name)

    target.parent.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)

    return target


def move_local(source: Path, target: Path) -> Path:
    """移动本地文件/目录，返回最终落点。"""

    source = Path(source)
    target = Path(target)

    if target.exists():
        target = unique_local_path(target.parent, target.name)

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))

    return target


def local_trash_root() -> Path:
    from . import settings

    root = settings.data_dir() / "trash"
    root.mkdir(parents=True, exist_ok=True)

    return root


def trash_local(path: Path) -> Path:
    """把本地文件/目录移进回收站，返回回收站里的位置。"""

    path = Path(path)
    bucket = local_trash_root() / time.strftime("%Y%m%d-%H%M%S")
    bucket.mkdir(parents=True, exist_ok=True)

    return move_local(path, bucket / path.name)


def remove_local(path: Path) -> None:
    """彻底删除本地文件/目录（撤销时用）。"""

    path = Path(path)

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def local_exists(path: Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


# ----------------------------------------------------------------------
# 远端文件操作（基于 SftpClient，但保持在这里以便统一测试）
# ----------------------------------------------------------------------

def unique_remote_path(sftp, directory: str, name: str) -> str:
    """远端目录里取一个不冲突的路径。"""

    import posixpath

    directory = directory.rstrip("/") or "/"
    candidate = posixpath.join(directory, name)

    if not sftp.exists(candidate):
        return candidate

    stem, suffix = posixpath.splitext(name)
    index = 2

    while True:
        candidate = posixpath.join(directory, f"{stem} ({index}){suffix}")

        if not sftp.exists(candidate):
            return candidate

        index += 1


def copy_remote(sftp, source: str, target: str, is_dir: bool) -> str:
    """远端内部复制（递归），返回最终落点。"""

    import posixpath

    if sftp.exists(target):
        target = unique_remote_path(sftp, posixpath.dirname(target), posixpath.basename(target))

    if not is_dir:
        payload = sftp.read_bytes(source)
        sftp.write_bytes(target, payload)

        return target

    sftp.mkdir_p(target)

    for entry in sftp.listdir(source):
        copy_remote(sftp, entry.path, posixpath.join(target, entry.name), entry.is_dir)

    return target


def move_remote(sftp, source: str, target: str) -> str:
    """远端内部移动：优先 rename，跨设备失败则退回复制+删除。"""

    import posixpath

    if sftp.exists(target):
        target = unique_remote_path(sftp, posixpath.dirname(target), posixpath.basename(target))

    try:
        sftp.rename(source, target)

        return target
    except Exception:
        pass

    is_dir = sftp.exists(source, is_dir=True)
    copied = copy_remote(sftp, source, target, is_dir)

    if is_dir:
        sftp.remove_tree(source)
    else:
        sftp.remove(source)

    return copied


def remote_trash_root(sftp) -> str:
    import posixpath

    return posixpath.join(sftp.home(), TRASH_DIR_NAME)


def trash_remote(sftp, path: str) -> str:
    """把远端文件/目录移进远端回收站，返回回收站里的位置。"""

    import posixpath

    bucket = posixpath.join(remote_trash_root(sftp), time.strftime("%Y%m%d-%H%M%S"))
    sftp.mkdir_p(bucket)

    return move_remote(sftp, path, posixpath.join(bucket, posixpath.basename(path)))


def remove_remote(sftp, path: str, is_dir: bool = False) -> None:
    """彻底删除远端文件/目录（撤销时用）。"""

    if is_dir:
        sftp.remove_tree(path)
    else:
        sftp.remove(path)


def plan_paste(items: list[ClipboardItem], target_side: str) -> list[str]:
    """给出一次粘贴会发生的动作描述（用于状态栏提示与测试）。"""

    actions = []

    for item in items:
        if item.side == SIDE_LOCAL and target_side == SIDE_REMOTE:
            actions.append(f"上传 {item.name} → Linux")
        elif item.side == SIDE_REMOTE and target_side == SIDE_LOCAL:
            actions.append(f"下载 {item.name} → Windows")
        elif item.side == SIDE_LOCAL:
            actions.append(f"本地复制 {item.name}")
        else:
            actions.append(f"远端复制 {item.name}")

    return actions
