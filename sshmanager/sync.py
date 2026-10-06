"""配置同步：把主机与（可选的）凭据同步到一个共享目录。

适合"团队共用一份主机清单"或"公司/家里两台机器共用配置"：

- ``export_package``：把当前主机写入共享目录的 ``pyssh-hosts.json``
- ``import_package``：读取共享目录并**合并**到本地（按主机 id 去重，取最近连接的版本）
- 默认不带凭据；显式开启后凭据以 DPAPI 加密后再落盘（换机器通常无法解密，
  换机器需要用同一账户导入后重新输入密码）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .credentials import CredentialStore
from .hosts import Host

PACKAGE_NAME = "pyssh-hosts.json"
PACKAGE_VERSION = 1


@dataclass
class SyncResult:
    """一次同步的统计结果。"""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    credentials: int = 0
    warnings: list[str] = field(default_factory=list)
    #: 远端主机 id → 本地实际保留的主机（凭据要按这个映射落库）
    id_map: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.added + self.updated + self.unchanged

    def summary(self, direction: str) -> str:
        text = f"{direction}：新增 {self.added}、更新 {self.updated}、未变 {self.unchanged}"

        if self.credentials:
            text += f"，凭据 {self.credentials} 条"

        if self.warnings:
            text += "\n" + "\n".join(self.warnings[:3])

        return text


def sync_folder_path(folder: Path) -> Path:
    return Path(folder) / PACKAGE_NAME


def export_package(
    folder: Path,
    hosts: list[Host],
    credentials: CredentialStore | None = None,
    include_credentials: bool = False,
) -> Path:
    """把主机清单写入共享目录，返回写入的文件路径。"""

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    payload: dict = {
        "version": PACKAGE_VERSION,
        "app": "PySSH Manager",
        "updated_at": time.time(),
        "hosts": [host.to_dict() for host in hosts],
    }

    if include_credentials and credentials is not None:
        secrets = {}

        for host in hosts:
            secret = credentials.get(host.id)

            if secret:
                secrets[host.id] = credentials._encrypt(secret)  # noqa: SLF001 - 复用同一套加密

        payload["credentials"] = secrets

    path = sync_folder_path(folder)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return path


def read_package(folder: Path) -> tuple[list[Host], dict[str, str], list[str]]:
    """读取共享目录里的清单，返回 ``(主机, 加密凭据, 警告)``。"""

    path = sync_folder_path(folder)
    warnings: list[str] = []

    if not path.exists():
        return [], {}, [f"共享目录里没有 {PACKAGE_NAME}"]

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [], {}, [f"读取失败：{error}"]

    if not isinstance(payload, dict):
        return [], {}, ["文件结构不正确"]

    if int(payload.get("version") or 0) > PACKAGE_VERSION:
        warnings.append("文件版本比当前应用新，可能有字段被忽略")

    hosts: list[Host] = []

    for item in payload.get("hosts") or []:
        if not isinstance(item, dict):
            continue

        try:
            host = Host.from_dict(item)
        except TypeError:
            continue

        if host.hostname:
            hosts.append(host)

    secrets = payload.get("credentials") or {}

    if not isinstance(secrets, dict):
        secrets = {}

    return hosts, secrets, warnings


def merge_hosts(local: list[Host], incoming: list[Host]) -> SyncResult:
    """把远端清单合并进本地列表（就地修改 ``local``）。"""

    result = SyncResult()

    by_id = {host.id: host for host in local}
    by_key = {(host.hostname, host.port, host.username): host for host in local}

    for host in incoming:
        existing = by_id.get(host.id)

        if existing is None:
            existing = by_key.get((host.hostname, host.port, host.username))

        if existing is None:
            local.append(host)
            by_id[host.id] = host
            by_key[(host.hostname, host.port, host.username)] = host
            result.id_map[host.id] = host.id
            result.added += 1
            continue

        # 凭据按远端 id 索引，要映射到本地实际保留的那个 id
        result.id_map[host.id] = existing.id

        # 同一台主机：取最近连接过的那份，保留本地 id 以免凭据失联
        if host.last_connected_at > existing.last_connected_at:
            keep_id, keep_tags = existing.id, list(existing.tags or [])

            existing.__dict__.update(host.__dict__)
            existing.id = keep_id

            if not existing.tags:
                existing.tags = keep_tags

            result.updated += 1
        else:
            result.unchanged += 1

    return result


def import_package(
    folder: Path,
    local_hosts: list[Host],
    credentials: CredentialStore | None = None,
) -> SyncResult:
    """从共享目录读取并合并到本地。"""

    incoming, secrets, warnings = read_package(folder)
    result = merge_hosts(local_hosts, incoming)
    result.warnings.extend(warnings)

    if credentials is not None and secrets:
        for remote_id, token in secrets.items():
            local_id = result.id_map.get(remote_id, remote_id)

            if credentials.get(local_id):
                continue

            try:
                credentials.set(local_id, credentials._decrypt(token))  # noqa: SLF001
                result.credentials += 1
            except Exception:
                # DPAPI 换机器解不开是预期情况，不计入错误
                continue

    return result
