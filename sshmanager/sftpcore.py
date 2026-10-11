"""SFTP 核心操作：目录列举与文件收发。

这一层不依赖 Qt，方便单独测试；界面通过 :mod:`sftpworker` 在后台线程里调用它。
"""

from __future__ import annotations

import posixpath
import stat as stat_module
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

CHUNK_SIZE = 64 * 1024


@dataclass
class TransferProgress:
    """传输进度快照（供界面显示百分比 / 速度 / 剩余时间）。"""

    task: str = "传输"
    name: str = ""
    done: int = 0
    total: int = 0
    batch_done: int = 0
    batch_total: int = 0
    index: int = 1
    count: int = 1
    speed: float = 0.0

    @property
    def percent(self) -> int:
        """当前文件（或整个任务）百分比。"""

        if self.total <= 0:
            return 100 if self.done > 0 else 0

        return max(0, min(100, int(self.done * 100 / self.total)))

    @property
    def batch_percent(self) -> int:
        if self.batch_total <= 0:
            return self.percent

        return max(0, min(100, int(self.batch_done * 100 / self.batch_total)))

    @property
    def eta(self) -> float | None:
        """预计剩余秒数；速度未知时返回 None。"""

        if self.speed <= 0 or self.total <= 0:
            return None

        remaining = max(0, self.batch_total or self.total) - (self.batch_done or self.done)

        return remaining / self.speed if remaining > 0 else 0.0

    @property
    def size_text(self) -> str:
        """已传 / 总量。"""

        if self.total <= 0:
            return format_size(self.done)

        return f"{format_size(self.done)}/{format_size(self.total)}"

    @property
    def batch_text(self) -> str:
        if self.batch_total > 0:
            return f"{format_size(self.batch_done)}/{format_size(self.batch_total)}"

        return ""

    @property
    def speed_text(self) -> str:
        if self.speed <= 0:
            return ""

        return f"{format_size(int(self.speed))}/s"

    @property
    def eta_text(self) -> str:
        eta = self.eta

        if eta is None:
            return ""

        if eta < 1:
            return "即将完成"

        if eta < 60:
            return f"剩 {int(eta)} 秒"

        if eta < 3600:
            return f"剩 {int(eta // 60)} 分 {int(eta % 60)} 秒"

        return f"剩 {eta / 3600:.1f} 小时"

    def summary(self) -> str:
        """一行摘要：任务 文件名 45%（3.2 MB/7.1 MB）· 1.8 MB/s · 剩 2 秒。"""

        parts = [f"{self.task} {self.name}".strip(), f"{self.percent}%（{self.size_text}）"]

        if self.count > 1:
            parts.insert(1, f"第 {self.index}/{self.count} 项")

        if self.batch_total > 0 and self.count > 1:
            parts.append(f"总 {self.batch_percent}%")

        if self.speed_text:
            parts.append(self.speed_text)

        if self.eta_text:
            parts.append(self.eta_text)

        return " · ".join(part for part in parts if part)


@dataclass
class RemoteEntry:
    """远端目录里的一条记录。"""

    name: str
    path: str
    size: int = 0
    modified: float = 0.0
    is_dir: bool = False
    is_link: bool = False

    @property
    def size_text(self) -> str:
        return "<目录>" if self.is_dir else format_size(self.size)

    @property
    def modified_text(self) -> str:
        if not self.modified:
            return ""

        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.modified))

    @property
    def kind_text(self) -> str:
        if self.is_dir:
            return "目录"

        if self.is_link:
            return "链接"

        return "文件"


def format_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    index = 0

    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1

    if index == 0:
        return f"{int(value)} {units[index]}"

    return f"{value:.1f} {units[index]}"


def normalize_remote_path(path: str) -> str:
    """把远端路径整理成绝对路径，去掉多余的斜杠和 ``..``。"""

    path = (path or "").strip() or "/"
    # 先合并开头的重复斜杠，否则 normpath 会保留 POSIX 的 "//" 特殊含义
    path = "/" + path.lstrip("/")
    normalized = posixpath.normpath(path)

    return normalized if normalized.startswith("/") else "/" + normalized


def parent_remote_path(path: str) -> str:
    normalized = normalize_remote_path(path)

    if normalized == "/":
        return "/"

    return normalize_remote_path(posixpath.dirname(normalized))


class SFTPError(Exception):
    """SFTP 操作失败。"""


class SftpClient:
    """对 paramiko SFTPClient 的薄封装。"""

    def __init__(self, ssh_client) -> None:
        self._ssh = ssh_client
        self._sftp: paramiko.SFTPClient | None = None

    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._sftp is not None:
            return

        try:
            self._sftp = self._ssh.open_sftp()
        except Exception as error:  # noqa: BLE001 - 统一转成自己的异常
            raise SFTPError(f"无法打开 SFTP 通道：{error}") from error

    def close(self) -> None:
        if self._sftp is None:
            return

        try:
            self._sftp.close()
        except Exception:
            pass

        self._sftp = None

    @property
    def opened(self) -> bool:
        return self._sftp is not None

    def _client(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise SFTPError("SFTP 尚未连接")

        return self._sftp

    # ------------------------------------------------------------------

    def home(self) -> str:
        try:
            return normalize_remote_path(self._client().normalize("."))
        except Exception:
            return "/"

    def listdir(self, path: str) -> list[RemoteEntry]:
        client = self._client()
        target = normalize_remote_path(path)

        try:
            attributes = client.listdir_attr(target)
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"读取目录失败：{target}（{error}）") from error

        entries: list[RemoteEntry] = []

        for attr in attributes:
            if attr.filename in (".", ".."):
                continue

            mode = attr.st_mode or 0
            is_link = stat_module.S_ISLNK(mode)
            is_dir = stat_module.S_ISDIR(mode)

            if is_link:
                # 链接指向目录时按目录处理，便于双击进入
                try:
                    is_dir = stat_module.S_ISDIR(client.stat(posixpath.join(target, attr.filename)).st_mode or 0)
                except Exception:
                    is_dir = False

            entries.append(
                RemoteEntry(
                    name=attr.filename,
                    path=posixpath.join(target, attr.filename),
                    size=attr.st_size or 0,
                    modified=attr.st_mtime or 0.0,
                    is_dir=is_dir,
                    is_link=is_link,
                )
            )

        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))

        return entries

    # ------------------------------------------------------------------

    def mkdir(self, path: str) -> None:
        try:
            self._client().mkdir(normalize_remote_path(path))
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"创建目录失败：{error}") from error

    def remove(self, path: str, is_dir: bool = False) -> None:
        target = normalize_remote_path(path)

        try:
            if is_dir:
                self._client().rmdir(target)
            else:
                self._client().remove(target)
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"删除失败：{target}（{error}）") from error

    def rename(self, old_path: str, new_path: str) -> None:
        try:
            self._client().rename(normalize_remote_path(old_path), normalize_remote_path(new_path))
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"重命名失败：{error}") from error

    def read_bytes(self, path: str) -> bytes:
        """读整个远端文件（用于远端内部复制）。"""

        try:
            with self._client().open(normalize_remote_path(path), "rb") as handle:
                return handle.read()
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"读取失败：{path}（{error}）") from error

    def write_bytes(self, path: str, payload: bytes) -> None:
        """写整个远端文件；必要时自动创建父目录。"""

        target = normalize_remote_path(path)
        parent = posixpath.dirname(target)

        if parent and parent != "/":
            self.mkdir_p(parent)

        try:
            with self._client().open(target, "wb") as handle:
                handle.write(payload)
        except Exception as error:  # noqa: BLE001
            raise SFTPError(f"写入失败：{target}（{error}）") from error

    # ------------------------------------------------------------------

    def exists(self, path: str, is_dir: bool | None = None) -> bool:
        """判断远端路径是否存在（可指定期望类型）。"""

        target = normalize_remote_path(path)

        try:
            attributes = self._client().stat(target)
        except Exception:
            return False

        if is_dir is None:
            return True

        mode = attributes.st_mode or 0

        return stat_module.S_ISDIR(mode) if is_dir else not stat_module.S_ISDIR(mode)

    def mkdir_p(self, path: str) -> None:
        """逐级创建远端目录（类似 ``mkdir -p``）。"""

        target = normalize_remote_path(path)
        parts = [part for part in target.split("/") if part]
        current = ""

        for part in parts:
            current = f"{current}/{part}"

            if not self.exists(current, is_dir=True):
                try:
                    self._client().mkdir(current)
                except Exception:
                    # 并发或已存在时忽略
                    pass

    # ------------------------------------------------------------------
    # 目录递归传输
    # ------------------------------------------------------------------

    def stat_size(self, path: str) -> int:
        """远端文件大小（字节）；取不到返回 0。"""

        try:
            return int(self._client().stat(normalize_remote_path(path)).st_size)
        except Exception:  # noqa: BLE001
            return 0

    def tree_size(self, remote_dir: str, limit: int = 4000) -> tuple[int, int]:
        """递归统计远端目录的 ``(文件数, 总字节)``，用于显示整体百分比。

        目录特别大时只扫到 ``limit`` 项就停（返回已知部分），避免传输前卡很久。
        """

        files = 0
        total = 0
        scanned = 0
        stack = [normalize_remote_path(remote_dir)]

        while stack:
            current = stack.pop()

            try:
                entries = self.listdir(current)
            except SFTPError:
                continue

            for entry in entries:
                if entry.is_dir:
                    stack.append(entry.path)
                    continue

                files += 1
                total += max(0, entry.size)
                scanned += 1

                if scanned >= limit:
                    return files, total

        return files, total

    def upload_tree(
        self,
        local_dir: Path,
        remote_dir: str,
        progress=None,
        cancelled=None,
        resume: bool = False,
        on_file=None,
    ) -> tuple[int, int]:
        """递归上传目录，返回 ``(文件数, 字节数)``。

        ``on_file(index, count, name)`` 在开始传每个文件时回调一次，
        便于界面重置「当前文件」的百分比。
        """

        local_dir = Path(local_dir)
        remote_dir = normalize_remote_path(remote_dir)
        files = 0
        total_bytes = 0
        self.mkdir_p(remote_dir)

        paths = [path for path in sorted(local_dir.rglob("*")) if path.is_file()]

        for index, path in enumerate(paths, start=1):
            if cancelled is not None and cancelled():
                raise SFTPError("已取消")

            relative = path.relative_to(local_dir)
            target = normalize_remote_path(f"{remote_dir}/{relative.as_posix()}")

            # 目录本身可能为空目录，需要显式建出来
            if relative.parent != Path("."):
                self.mkdir_p(normalize_remote_path(f"{remote_dir}/{relative.parent.as_posix()}"))

            if on_file is not None:
                on_file(index, len(paths), path.name)

            written, _resumed = self.upload(path, target, progress=progress, cancelled=cancelled, resume=resume)
            files += 1
            total_bytes += written

        # 空目录也要建
        for path in sorted(local_dir.rglob("*")):
            if path.is_dir():
                relative = path.relative_to(local_dir)
                self.mkdir_p(normalize_remote_path(f"{remote_dir}/{relative.as_posix()}"))

        return files, total_bytes

    def download_tree(
        self,
        remote_dir: str,
        local_dir: Path,
        progress=None,
        cancelled=None,
        resume: bool = False,
        on_file=None,
        counter: list | None = None,
    ) -> tuple[int, int]:
        """递归下载目录，返回 ``(文件数, 字节数)``。"""

        remote_dir = normalize_remote_path(remote_dir)
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        files = 0
        total_bytes = 0
        state = counter if counter is not None else [0, 0]

        for entry in self.listdir(remote_dir):
            if cancelled is not None and cancelled():
                raise SFTPError("已取消")

            if entry.is_dir:
                sub_files, sub_bytes = self.download_tree(
                    entry.path,
                    local_dir / entry.name,
                    progress=progress,
                    cancelled=cancelled,
                    resume=resume,
                    on_file=on_file,
                    counter=state,
                )
                files += sub_files
                total_bytes += sub_bytes
                continue

            state[0] += 1

            if on_file is not None:
                on_file(state[0], state[1], entry.name)

            written, _resumed = self.download(
                entry.path,
                local_dir / entry.name,
                progress=progress,
                cancelled=cancelled,
                resume=resume,
            )
            files += 1
            total_bytes += written

        return files, total_bytes

    def remove_tree(self, remote_dir: str) -> int:
        """递归删除远端目录，返回删除的条目数。"""

        remote_dir = normalize_remote_path(remote_dir)
        removed = 0

        for entry in self.listdir(remote_dir):
            if entry.is_dir:
                removed += self.remove_tree(entry.path)
            else:
                self.remove(entry.path)
                removed += 1

        self.remove(remote_dir, is_dir=True)

        return removed + 1

    # ------------------------------------------------------------------

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress=None,
        cancelled=None,
        resume: bool = False,
    ) -> tuple[int, bool]:
        """把远端文件下载到本地。

        返回 ``(本次写入字节数, 是否为续传)``。
        ``resume=True`` 时会比较本地已有文件与远端大小，只拉取缺失的尾部：
        写入临时 ``.part`` 文件，成功后原子替换为目标文件。
        """

        client = self._client()
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        with client.open(normalize_remote_path(remote_path), "rb") as source:
            try:
                total = source.stat().st_size or 0
            except Exception:
                total = 0

            offset = 0
            partial = local_path.with_name(local_path.name + ".part")
            resuming = False

            if resume:
                if partial.exists():
                    offset = partial.stat().st_size
                elif local_path.exists():
                    offset = local_path.stat().st_size

                if total and offset >= total:
                    # 已经完整，直接把 part 转正
                    if partial.exists():
                        partial.replace(local_path)
                        return 0, True

                    return 0, True

                if offset > 0:
                    resuming = True
                    source.seek(offset)

            mode = "ab" if resuming else "wb"
            written = 0

            with open(partial, mode) as target:
                while True:
                    if cancelled is not None and cancelled():
                        raise SFTPError("已取消")

                    chunk = source.read(CHUNK_SIZE)

                    if not chunk:
                        break

                    target.write(chunk)
                    written += len(chunk)

                    if progress is not None:
                        progress(offset + written, total)

        # 只有跑完才替换目标文件，避免半截文件覆盖已有内容
        partial.replace(local_path)

        return written, resuming

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        progress=None,
        cancelled=None,
        resume: bool = False,
    ) -> tuple[int, bool]:
        """把本地文件上传到远端，返回 ``(本次写入字节数, 是否为续传)``。"""

        client = self._client()
        local_path = Path(local_path)
        total = local_path.stat().st_size
        remote_path = normalize_remote_path(remote_path)
        offset = 0
        resuming = False

        if resume:
            try:
                remote_size = client.stat(remote_path).st_size or 0
            except Exception:
                remote_size = 0

            if total and remote_size >= total:
                return 0, True

            if remote_size > 0:
                offset = remote_size
                resuming = True

        written = 0

        with open(local_path, "rb") as source:
            if offset:
                source.seek(offset)

            with client.open(remote_path, "ab" if resuming else "wb") as target:
                while True:
                    if cancelled is not None and cancelled():
                        raise SFTPError("已取消")

                    chunk = source.read(CHUNK_SIZE)

                    if not chunk:
                        break

                    target.write(chunk)
                    written += len(chunk)

                    if progress is not None:
                        progress(offset + written, total)

        return written, resuming
