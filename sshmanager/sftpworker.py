"""SFTP 后台线程：所有网络操作都在这里执行，界面只发指令、收信号。

paramiko 的 SFTP 调用是阻塞的，放进 GUI 线程会卡住界面；这里用一条
任务队列 + 单个工作线程，串行执行所有请求。
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .hosts import Host
from .sftpcore import SftpClient
from .sshclient import SSHClient


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

    #: 致命错误（连接阶段）
    failed = Signal(str)

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

    def request_download(self, remote_path: str, local_path: Path, is_dir: bool = False) -> None:
        self._jobs.put(("download", remote_path, Path(local_path), is_dir))

    def request_upload(self, local_path: Path, remote_path: str, is_dir: bool = False) -> None:
        self._jobs.put(("upload", Path(local_path), remote_path, is_dir))

    def request_mkdir(self, path: str) -> None:
        self._jobs.put(("mkdir", path))

    def request_remove(self, path: str, is_dir: bool = False) -> None:
        self._jobs.put(("remove", path, is_dir))

    def request_rename(self, old_path: str, new_path: str) -> None:
        self._jobs.put(("rename", old_path, new_path))

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
                name = Path(remote_path).name or remote_path

                def on_progress(done: int, total: int, label: str = name) -> None:
                    self.progress.emit(f"下载 {label}", done, total)

                if is_dir:
                    files, size = self._sftp.download_tree(
                        remote_path,
                        local_path,
                        progress=on_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
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

                return

            if action == "upload":
                local_path, remote_path, is_dir = job[1], job[2], job[3]

                def on_upload_progress(done: int, total: int, label: str = local_path.name) -> None:
                    self.progress.emit(f"上传 {label}", done, total)

                if is_dir:
                    files, size = self._sftp.upload_tree(
                        local_path,
                        remote_path,
                        progress=on_upload_progress,
                        cancelled=self._cancel.is_set,
                        resume=self.resume,
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

            if action == "rename":
                self._sftp.rename(job[1], job[2])
                self.task_finished.emit("重命名", True, f"{job[1]} → {job[2]}")
                return

            self.task_finished.emit(action, False, f"未知任务：{action}")
        except Exception as error:  # noqa: BLE001 - 每个任务的失败单独反馈
            self.task_finished.emit(action, False, f"{error}")
