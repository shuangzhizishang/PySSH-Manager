"""SFTP 后台线程：所有网络操作都在这里执行，界面只发指令、收信号。

paramiko 的 SFTP 调用是阻塞的，放进 GUI 线程会卡住界面；这里用一条
任务队列 + 单个工作线程，串行执行所有请求。
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .fileops import SIDE_LOCAL, SIDE_REMOTE, copy_remote, move_remote, trash_remote
from .hosts import Host
from .sftpcore import SftpClient, TransferProgress
from .sshclient import SSHClient


def _local_tree_size(path: Path) -> tuple[int, int]:
    """本地目录的 ``(文件数, 总字节)``。"""

    files = 0
    total = 0

    for item in Path(path).rglob("*"):
        if item.is_file():
            files += 1

            try:
                total += item.stat().st_size
            except OSError:
                pass

    return files, total


class SftpWorker(QThread):
    """按需建立独立 SSH/SFTP 连接，并串行处理界面发来的任务。"""

    #: 连接成功，附带远端家目录
    connected = Signal(str)

    connecting = Signal()
    disconnected = Signal()

    #: 目录内容 (路径, 条目列表)
    listing = Signal(str, object)

    #: 任务结束 (任务名, 是否成功, 说明)
    task_finished = Signal(str, bool, str)

    #: 传输进度 (任务名, 已完成字节, 总字节)
    progress = Signal(str, int, int)

    #: 详细传输进度：:class:`~sshmanager.sftpcore.TransferProgress`（含百分比 / 速度 / 剩余时间）
    progress_detail = Signal(object)

    #: 致命错误（连接阶段）
    failed = Signal(str)

    #: (批次号, 新建的远端路径列表, 移动的 (原路径, 现路径) 列表) —— 供撤销栈使用
    batch_result = Signal(int, object, object)

    def __init__(self, host: Host, credential: str | None = None, jump: SSHClient | None = None, parent=None) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential
        self.jump = jump

        self.client = SSHClient(host)
        self._sftp: SftpClient | None = None
        self._jobs: "queue.Queue[tuple | None]" = queue.Queue()
        self._abort = threading.Event()
        self._cancel = threading.Event()

        #: 是否对文件传输启用断点续传（由界面切换）
        self.resume = False

    # ------------------------------------------------------------------
    # 界面接口
    # ------------------------------------------------------------------

    def request_list(self, path: str) -> None:
        self._jobs.put(("list", path))

    def request_download(self, remote_path: str, local_path: Path, is_dir: bool = False, batch: int = 0) -> None:
        self._jobs.put(("download", remote_path, Path(local_path), is_dir, batch))

    def request_upload(self, local_path: Path, remote_path: str, is_dir: bool = False, batch: int = 0) -> None:
        self._jobs.put(("upload", Path(local_path), remote_path, is_dir, batch))

    def request_mkdir(self, path: str) -> None:
        self._jobs.put(("mkdir", path))

    def request_remove(self, path: str, is_dir: bool = False) -> None:
        self._jobs.put(("remove", path, is_dir))

    def request_remove_any(self, path: str) -> None:
        """按实际情况删除：目录递归删、文件直接删（撤销清理用）。"""

        self._jobs.put(("remove_any", path))

    def request_rename(self, old_path: str, new_path: str) -> None:
        self._jobs.put(("rename", old_path, new_path))

    def request_copy(self, source: str, target: str, is_dir: bool = False, batch: int = 0) -> None:
        """远端内部复制。"""

        self._jobs.put(("copy", source, target, is_dir, batch))

    def request_move(self, source: str, target: str, batch: int = 0) -> None:
        """远端内部移动（rename，失败退回复制+删除）。"""

        self._jobs.put(("move", source, target, batch))

    def request_trash(self, path: str, is_dir: bool = False, batch: int = 0) -> None:
        """删除到远端回收站（可撤销）。"""

        self._jobs.put(("trash", path, is_dir, batch))

    def request_restore(self, pairs: list[tuple[str, str]], label: str = "撤销") -> None:
        """撤销：把 (现路径) 移回 (原路径)。"""

        self._jobs.put(("restore", pairs, label))

    def request_disconnect(self) -> None:
        self.stop()

    def stop(self) -> None:
        """请求退出：中断当前任务并唤醒队列。"""

        self._abort.set()
        self._cancel.set()
        self._jobs.put(None)

    def cancel_current(self) -> None:
        """取消正在进行的传输（不影响后续任务）。"""

        self._cancel.set()

    def clear_cancel(self) -> None:
        self._cancel.clear()

    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - 线程主循环，分支较多但线性
        self.connecting.emit()

        try:
            self.client.connect(
                hostname=self.host.hostname,
                port=self.host.port,
                username=self.host.username,
                password=self.credential if self.host.auth_type == "password" else None,
                key_path=self.host.key_path if self.host.auth_type == "key" else None,
                passphrase=self.credential if self.host.auth_type == "key" else None,
                verify_host_key=self.host.verify_host_key,
                term_type=self.host.term_type,
                jump=self.jump,
            )

            self._sftp = SftpClient(self.client)
            self._sftp.open()
            home = self._sftp.home()
        except Exception as error:  # noqa: BLE001 - 任何失败都要反馈到界面
            self.failed.emit(f"{error}")
            self.client.close()
            return

        self.connected.emit(home)

        try:
            while not self._abort.is_set():
                job = self._jobs.get()

                if job is None:
                    break

                self._run_job(job)
        finally:
            if self._sftp is not None:
                self._sftp.close()

            self.client.close()
            self.disconnected.emit()

    # ------------------------------------------------------------------

    def _make_tracker(self, task: str, name: str, total_files: int, total_bytes: int):
        """构造进度跟踪器：累计批次字节、估算速度，并发出粗细两种进度信号。

        返回的函数签名是 ``(done, total)``（当前文件已传/总字节）；
        另外挂了 ``file_started(index, count, name)`` 用于切换「当前文件」。
        """

        state = {
            "index": 1,
            "count": max(1, total_files or 1),
            "name": name,
            "batch_done": 0,
            "current_total": 0,
            "last_done": 0,
            "started": time.monotonic(),
            "speed": 0.0,
        }

        def report(done: int, total: int) -> None:
            # 当前文件字节变化 = 批次增量的来源
            delta = max(0, done - state["last_done"])
            state["last_done"] = done
            state["current_total"] = total or state["current_total"]
            state["batch_done"] += delta

            elapsed = max(0.001, time.monotonic() - state["started"])

            # 速度用滑动平均，避免瞬间抖动
            instant = state["batch_done"] / elapsed
            state["speed"] = instant if state["speed"] <= 0 else state["speed"] * 0.7 + instant * 0.3

            self.progress.emit(f"{task} {state['name']}", done, total)
            self.progress_detail.emit(
                TransferProgress(
                    task=task,
                    name=state["name"],
                    done=done,
                    total=state["current_total"],
                    batch_done=state["batch_done"],
                    batch_total=total_bytes,
                    index=state["index"],
                    count=state["count"],
                    speed=state["speed"],
                )
            )

        def file_started(index: int, count: int, current_name: str) -> None:
            state["index"] = index
            state["count"] = max(1, count)
            state["name"] = current_name
            state["last_done"] = 0
            state["current_total"] = 0

        report.file_started = file_started  # type: ignore[attr-defined]

        return report

    def _run_job(self, job: tuple) -> None:
        action = job[0]
        self._cancel.clear()

        try:
            if action == "list":
                entries = self._sftp.listdir(job[1])
                self.listing.emit(job[1], entries)
                return

            if action == "download":
                remote_path, local_path, is_dir = job[1], job[2], job[3]
                batch = job[4] if len(job) > 4 else 0
                name = Path(remote_path).name or remote_path

                # 先算总量，进度条才能显示有意义的百分比
                total_files, total_bytes = (0, 0)

                if is_dir:
                    total_files, total_bytes = self._sftp.tree_size(remote_path)
                else:
                    try:
                        total_bytes = max(0, self._sftp.stat_size(remote_path))
                    except Exception:
                        total_bytes = 0

                tracker = self._make_tracker("下载", name, total_files, total_bytes)

                def on_progress(done: int, total: int) -> None:
                    tracker(done, total)

                if is_dir:
                    files, size = self._sftp.download_tree(
                        remote_path,
                        local_path,
                        progress=on_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
                        on_file=tracker.file_started,
                        counter=[0, total_files],
                    )
                    self.task_finished.emit("下载目录", True, f"{name}（{files} 个文件，{size} 字节）")
                else:
                    size, resumed = self._sftp.download(
                        remote_path,
                        local_path,
                        progress=on_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
                    )
                    suffix = "（续传）" if resumed else ""
                    self.task_finished.emit("下载", True, f"{local_path.name}{suffix}（{size} 字节）")

                if batch:
                    # 下载只在本地新建内容
                    self.batch_result.emit(batch, [(SIDE_LOCAL, str(local_path))], [])

                return

            if action == "upload":
                local_path, remote_path, is_dir = job[1], job[2], job[3]
                batch = job[4] if len(job) > 4 else 0

                total_files, total_bytes = (0, 0)

                if is_dir:
                    total_files, total_bytes = _local_tree_size(local_path)
                else:
                    try:
                        total_bytes = local_path.stat().st_size
                    except OSError:
                        total_bytes = 0

                tracker = self._make_tracker("上传", local_path.name, total_files, total_bytes)

                def on_upload_progress(done: int, total: int) -> None:
                    tracker(done, total)

                if is_dir:
                    files, size = self._sftp.upload_tree(
                        local_path,
                        remote_path,
                        progress=on_upload_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
                        on_file=tracker.file_started,
                    )
                    self.task_finished.emit("上传目录", True, f"{local_path.name}（{files} 个文件，{size} 字节）")
                else:
                    size, resumed = self._sftp.upload(
                        local_path,
                        remote_path,
                        progress=on_upload_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
                    )
                    suffix = "（续传）" if resumed else ""
                    self.task_finished.emit("上传", True, f"{remote_path}{suffix}（{size} 字节）")

                if batch:
                    # 上传只在远端新建内容
                    self.batch_result.emit(batch, [(SIDE_REMOTE, remote_path)], [])

                return

            if action == "mkdir":
                self._sftp.mkdir(job[1])
                self.task_finished.emit("新建目录", True, job[1])
                return

            if action == "remove":
                path, is_dir = job[1], bool(job[2])

                if is_dir:
                    removed = self._sftp.remove_tree(path)
                    self.task_finished.emit("删除目录", True, f"{path}（{removed} 项）")
                else:
                    self._sftp.remove(path, is_dir=False)
                    self.task_finished.emit("删除", True, path)

                return

            if action == "remove_any":
                path = job[1]

                if self._sftp.exists(path, is_dir=True):
                    self._sftp.remove_tree(path)
                    self.task_finished.emit("删除目录", True, path)
                elif self._sftp.exists(path):
                    self._sftp.remove(path, is_dir=False)
                    self.task_finished.emit("删除", True, path)
                else:
                    self.task_finished.emit("删除", True, f"{path}（已不存在）")

                return

            if action == "rename":
                self._sftp.rename(job[1], job[2])
                self.task_finished.emit("重命名", True, f"{job[1]} → {job[2]}")
                return

            if action == "copy":
                source, target, is_dir = job[1], job[2], bool(job[3])
                batch = job[4] if len(job) > 4 else 0
                final = copy_remote(self._sftp, source, target, is_dir)
                self.task_finished.emit("远端复制", True, final)

                if batch:
                    self.batch_result.emit(batch, [(SIDE_REMOTE, final)], [])

                return

            if action == "move":
                source, target = job[1], job[2]
                batch = job[3] if len(job) > 3 else 0
                final = move_remote(self._sftp, source, target)
                self.task_finished.emit("远端移动", True, f"{source} → {final}")

                if batch:
                    self.batch_result.emit(batch, [], [(SIDE_REMOTE, source, final)])

                return

            if action == "trash":
                path, _is_dir = job[1], bool(job[2])
                batch = job[3] if len(job) > 3 else 0
                final = trash_remote(self._sftp, path)
                self.task_finished.emit("删除到回收站", True, f"{path}（可撤销）")

                if batch:
                    self.batch_result.emit(batch, [], [(SIDE_REMOTE, path, final)])

                return

            if action == "restore":
                pairs = job[1]
                label = job[2] if len(job) > 2 else "撤销"
                errors = []

                for current, original in reversed(list(pairs)):
                    try:
                        if self._sftp.exists(original):
                            self._sftp.remove_tree(original) if self._sftp.exists(
                                original, is_dir=True
                            ) else self._sftp.remove(original)

                        parent = original.rsplit("/", 1)[0]

                        if parent:
                            self._sftp.mkdir_p(parent)

                        self._sftp.rename(current, original)
                    except Exception as error:  # noqa: BLE001
                        errors.append(f"{original}: {error}")

                self.task_finished.emit(
                    label,
                    not errors,
                    "已恢复" if not errors else f"部分失败：{errors[0]}",
                )

                return

            self.task_finished.emit(action, False, f"未知任务：{action}")
        except Exception as error:  # noqa: BLE001 - 每个任务的失败单独反馈
            self.task_finished.emit(action, False, f"{error}")
