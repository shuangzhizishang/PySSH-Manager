"""主机列表：数据模型与 JSON 持久化。"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import settings


@dataclass
class Host:
    """一条 SSH 连接配置。

    注意：这里**不保存密码**，密码/口令统一放在 :class:`~sshmanager.credentials.CredentialStore`。
    """

    label: str = ""
    hostname: str = ""
    port: int = 22
    username: str = "root"

    #: "password" | "key"
    auth_type: str = "password"

    #: 私钥文件路径（auth_type == "key" 时使用）
    key_path: str = ""

    #: 是否校验服务器指纹（known_hosts）
    verify_host_key: bool = True

    #: 跳板机（另一个 Host 的 id），留空表示直连
    jump_host_id: str = ""

    #: 本地端口转发规则（存 dict，避免循环导入；用 forwarding.ForwardRule 解析）
    forwards: list = field(default_factory=list)

    #: 标签 / 分组（列表里第一个也作为侧边栏的分组名）
    tags: list = field(default_factory=list)

    @property
    def group(self) -> str:
        """侧边栏分组名。"""

        for tag in self.tags or []:
            text = str(tag).strip()

            if text:
                return text

        return "未分组"

    #: 外观：终端字体与配色
    font_family: str = settings.DEFAULT_FONT_FAMILY
    font_size: int = settings.DEFAULT_FONT_SIZE

    #: 可选：覆盖远程 TERM 类型
    term_type: str = "xterm-256color"

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    last_connected_at: float = 0.0

    @property
    def display_name(self) -> str:
        return self.label or self.hostname or "未命名主机"

    @property
    def target(self) -> str:
        return f"{self.username}@{self.hostname}:{self.port}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Host":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        clean = {key: value for key, value in data.items() if key in known}
        host = cls(**clean)

        try:
            host.port = int(host.port)
        except (TypeError, ValueError):
            host.port = 22

        try:
            host.font_size = int(host.font_size)
        except (TypeError, ValueError):
            host.font_size = settings.DEFAULT_FONT_SIZE

        return host


class HostStore:
    """主机列表的读写。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else settings.hosts_file()
        self.hosts: list[Host] = []
        self.load()

    # ------------------------------------------------------------------

    def load(self) -> None:
        self.hosts = []

        if not self.path.exists():
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(raw, list):
            return

        for item in raw:
            if isinstance(item, dict):
                try:
                    self.hosts.append(Host.from_dict(item))
                except TypeError:
                    continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [host.to_dict() for host in self.hosts]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------

    def get(self, host_id: str) -> Host | None:
        for host in self.hosts:
            if host.id == host_id:
                return host
        return None

    def add(self, host: Host) -> Host:
        self.hosts.append(host)
        self.save()
        return host

    def update(self, host: Host) -> None:
        for index, existing in enumerate(self.hosts):
            if existing.id == host.id:
                self.hosts[index] = host
                self.save()
                return

        self.hosts.append(host)
        self.save()

    def remove(self, host_id: str) -> None:
        self.hosts = [host for host in self.hosts if host.id != host_id]
        self.save()

    def mark_connected(self, host_id: str) -> None:
        host = self.get(host_id)

        if host is None:
            return

        host.last_connected_at = time.time()
        self.save()

    def sorted_hosts(self) -> list[Host]:
        """最近连接的排在前面。"""

        return sorted(self.hosts, key=lambda host: host.last_connected_at, reverse=True)


def migrate_legacy_host(
    store: HostStore,
    credentials,
    host: str,
    port: int,
    username: str,
    password: str,
) -> Host:
    """把老版本 main.py 里硬编码的连接信息导入主机列表。"""

    for existing in store.hosts:
        if (existing.hostname, existing.port, existing.username) == (host, port, username):
            return existing

    entry = Host(label=host, hostname=host, port=port, username=username, auth_type="password")
    store.add(entry)

    if password:
        credentials.set(entry.id, password)

    return entry
