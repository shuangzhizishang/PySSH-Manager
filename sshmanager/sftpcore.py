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

    def upload_tree(
        self,
        local_dir: Path,
        remote_dir: str,
        progress=None,
        cancelled=None,
        resume: bool = False,
    ) -> tuple[int, int]:
        """递归上传目录，返回 ``(文件数, 字节数)``。"""

        local_dir = Path(local_dir)
        remote_dir = normalize_remote_path(remote_dir)
        files = 0
        total_bytes = 0
        self.mkdir_p(remote_dir)

        for path in sorted(local_dir.rglob("*")):
            if cancelled is not None and cancelled():
                raise SFTPError("已取消")

            relative = path.relative_to(local_dir)
            target = normalize_remote_path(f"{remote_dir}/{relative.as_posix()}")

            if path.is_dir():
                self.mkdir_p(target)
                continue

            written, _resumed = self.upload(path, target, progress=progress, cancelled=cancelled, resume=resume)
            files += 1
            total_bytes += written

        return files, total_bytes

    def download_tree(
        self,
        remote_dir: str,
        local_dir: Path,
        progress=None,
        cancelled=None,
        resume: bool = False,
    ) -> tuple[int, int]:
        """递归下载目录，返回 ``(文件数, 字节数)``。"""

        remote_dir = normalize_remote_path(remote_dir)
        local_dir = Path(local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)
        files = 0
        total_bytes = 0

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
                )
                files += sub_files
                total_bytes += sub_bytes
                continue

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
