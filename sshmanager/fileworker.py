"""本地文件操作后台线程：复制 / 移动 / 回收站 / 删除 / 新建目录。

SFTP 面板里"本地 ↔ 本地"的复制剪切如果放在 UI 线程，遇到大目录会卡住界面。
这里用与 :class:`~sshmanager.sftpworker.SftpWorker` 相同的任务队列模型：
界面发指令、线程执行、信号回传结果（含撤销所需的路径信息）。
"""

from __future__ import annotations

import queue
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .fileops import (
    SIDE_LOCAL,
    copy_local,
    move_local,
    remove_local,
    trash_local,
    unique_local_path,
)


class LocalFileWorker(QThread):
    """串行执行本地文件任务。"""

    #: (任务名, 是否成功, 说明)
    task_finished = Signal(str, bool, str)

    #: (任务名, 已完成项, 总项)
    progress = Signal(str, int, int)

    #: (批次号, 新建的路径列表, 移动的 (原路径, 现路径) 列表) —— 供撤销栈使用
    batch_result = Signal(int, object, object)

    #: (任务名, 是否成功, 说明) —— 撤回（撤销）结果
    undo_finished = Signal(str, bool, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._jobs: "queue.Queue[tuple | None]" = queue.Queue()
        self._abort = threading.Event()
        self._cancel = threading.Event()

    # ------------------------------------------------------------------
    # 界面接口
    # ------------------------------------------------------------------

    def submit_copy(self, sources: list[Path], target_dir: Path, batch: int = 0) -> None:
        self._jobs.put(("copy", [Path(p) for p in sources], Path(target_dir), batch))

    def submit_move(self, sources: list[Path], target_dir: Path, batch: int = 0) -> None:
        self._jobs.put(("move", [Path(p) for p in sources], Path(target_dir), batch))

    def submit_trash(self, sources: list[Path], batch: int = 0) -> None:
        self._jobs.put(("trash", [Path(p) for p in sources], None, batch))

    def submit_delete(self, paths: list[Path]) -> None:
        self._jobs.put(("delete", [Path(p) for p in paths], None, 0))

    def submit_mkdir(self, path: Path) -> None:
        self._jobs.put(("mkdir", [Path(path)], None, 0))

    def submit_restore(self, pairs: list[tuple[Path, Path]], label: str = "撤销") -> None:
        """撤销：把 (现路径) 移回 (原路径)。"""

        self._jobs.put(("restore", pairs, None, 0, label))

    def stop(self) -> None:
        self._abort.set()
        self._jobs.put(None)

    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - 线程主循环
        while not self._abort.is_set():
            job = self._jobs.get()

            if job is None:
                break

            self._run_job(job)

    def _run_job(self, job: tuple) -> None:
        action = job[0]

        if action == "restore":
            self._run_restore(job[1], job[4] if len(job) > 4 else "撤销")
            return

        sources, target_dir, batch = job[1], job[2], job[3]
        created: list[str] = []
        moved: list[tuple[str, str]] = []

        try:
            if action == "mkdir":
                path = sources[0]
                path.mkdir(parents=True, exist_ok=True)
                created.append(str(path))
                self.task_finished.emit("新建目录", True, str(path))
            elif action in ("copy", "move"):
                total = len(sources)

                for index, source in enumerate(sources, start=1):
                    target = unique_local_path(Path(target_dir), source.name)

                    if action == "copy":
                        final = copy_local(source, target)
                        created.append(str(final))
                    else:
                        final = move_local(source, target)
                        moved.append((str(source), str(final)))

                    self.progress.emit("复制" if action == "copy" else "移动", index, total)

                self.task_finished.emit(
                    "复制" if action == "copy" else "移动",
                    True,
                    f"{len(sources)} 项 → {target_dir}",
                )
            elif action == "trash":
                total = len(sources)

                for index, source in enumerate(sources, start=1):
                    final = trash_local(source)
                    moved.append((str(source), str(final)))
                    self.progress.emit("删除", index, total)

                self.task_finished.emit("删除到回收站", True, f"{len(sources)} 项（可撤销）")
            elif action == "delete":
                total = len(sources)

                for index, source in enumerate(sources, start=1):
                    remove_local(source)
                    self.progress.emit("清理", index, total)

                self.task_finished.emit("清理回收站", True, f"{len(sources)} 项")
            else:
                self.task_finished.emit(action, False, f"未知任务：{action}")
                return
        except Exception as error:  # noqa: BLE001 - 每个任务的失败单独反馈
            self.task_finished.emit(action, False, f"{error}")
            return

        if batch:
            self.batch_result.emit(
                batch,
                [(SIDE_LOCAL, path) for path in created],
                [(SIDE_LOCAL, source, target) for source, target in moved],
            )

    def _run_restore(self, pairs: list[tuple[Path, Path]], label: str) -> None:
        restored = 0
        errors = []

        for current, original in reversed(list(pairs)):
            try:
                original_parent = Path(original).parent
                original_parent.mkdir(parents=True, exist_ok=True)

                if Path(original).exists():
                    remove_local(Path(original))

                shutil.move(str(current), str(original))
                restored += 1
            except Exception as error:  # noqa: BLE001
                errors.append(f"{Path(original).name}: {error}")

        ok = not errors
        message = f"已恢复 {restored} 项" + (f"，失败 {len(errors)} 项" if errors else "")
        self.undo_finished.emit(label, ok, message)
