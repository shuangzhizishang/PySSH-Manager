"""主机导入 / 导出：OpenSSH config 与 JSON 两种格式。

- ``import_openssh_config``：解析 ``~/.ssh/config`` 风格的文本
- ``export_openssh_config``：导出为可直接使用的 ssh config 文本
- ``export_json`` / ``import_json``：完整往返（可选带加密凭据）

默认导出**不含明文密码**；需要时可显式要求把凭据用 DPAPI 加密后一起导出，
这样换机器前可以整体搬走（注意：DPAPI 加密的数据只能在原用户/原机器解密）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .credentials import CredentialStore
from .hosts import Host

EXPORT_VERSION = 1

#: ssh config 中常见的开关，导入时忽略（不映射到应用配置）
_IGNORED_PREFIXES = (
    "identityagent",
    "forwardagent",
    "forwardx11",
    "userknownhostsfile",
    "serveraliveinterval",
    "serveralivecountmax",
    "compression",
    "tcpkeepalive",
    "hashknownhosts",
    "pubkeyauthentication",
    "passwordauthentication",
    "challengeresponseauthentication",
    "localforward",
    "remoteforward",
    "dynamicforward",
    "proxycommand",
    "addkeystoagent",
    "identitiesonly",
    "preferredauthentications",
)


@dataclass
class ImportResult:
    hosts: list[Host]
    skipped: list[str]
    warnings: list[str]
    #: 别名 → ProxyJump 目标别名（由调用方解析成真实主机 id）
    proxy_jumps: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.hosts)


# ----------------------------------------------------------------------
# OpenSSH config
# ----------------------------------------------------------------------

def _split_directive(line: str) -> tuple[str, str]:
    """把 ``Key value`` 或 ``Key=value`` 拆成 (key, value)。"""

    if "=" in line and (line.index("=") < line.index(" ") if " " in line else True):
        key, _, value = line.partition("=")
    else:
        parts = line.split(None, 1)
        key = parts[0]
        value = parts[1] if len(parts) > 1 else ""

    return key.strip().lower(), value.strip()


def parse_openssh_config(text: str) -> ImportResult:
    """解析 ssh config 文本，返回可导入的主机列表。

    只处理具体的 Host 块（跳过 ``*`` 通配），保留 HostName / Port / User /
    IdentityFile / HostKeyAlias 等信息。
    """

    hosts: list[Host] = []
    skipped: list[str] = []
    warnings: list[str] = []

    current: dict | None = None
    proxy_jumps: dict[str, str] = {}

    def flush() -> None:
        nonlocal current

        if current is None:
            return

        alias = current.get("alias", "")
        hostname = current.get("hostname") or alias

        if current.get("skip"):
            current = None
            return

        if not hostname:
            skipped.append(alias or "(空 Host)")
            current = None
            return

        key_path = current.get("identityfile", "")
        host = Host(
            label=alias,
            hostname=hostname,
            port=current.get("port", 22),
            username=current.get("user", "root") or "root",
            auth_type="key" if key_path else "password",
            key_path=key_path,
            verify_host_key=current.get("stricthostkeychecking", "yes").lower() not in ("no", "false", "off"),
        )

        # 用别名当分组：``prod-web`` 归到 ``prod``
        if alias:
            host.tags = [alias.split("-")[0]] if "-" in alias else [alias]

        jump = current.get("proxyjump")

        if jump:
            proxy_jumps[alias] = jump.split(",")[0].strip()

        hosts.append(host)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        key, value = _split_directive(line)

        if key == "host":
            flush()
            names = [name for name in value.split() if name]

            if not names:
                current = None
                skipped.append("(空 Host)")
                continue

            if any("*" in name or "?" in name for name in names):
                # 通配块里的 StrictHostKeyChecking 仍要读，但它本身不是一条主机
                skipped.append(value)
                current = {"alias": names[0], "skip": True}
                continue

            current = {"alias": names[0]}

            if len(names) > 1:
                warnings.append(f"Host {value} 有多个别名，只取第一个 {names[0]}")

            continue

        if current is None:
            continue

        # StrictHostKeyChecking 即使出现在通配块里也要生效（ssh 的全局默认）
        if key == "stricthostkeychecking":
            current[key] = value
            continue

        if current.get("skip") or key in _IGNORED_PREFIXES:
            continue

        if key == "hostname":
            current["hostname"] = value
        elif key == "port":
            try:
                current["port"] = int(value)
            except ValueError:
                warnings.append(f"端口不是数字，已忽略：{value}")
        elif key == "user":
            current["user"] = value
        elif key == "identityfile":
            current["identityfile"] = value.replace("%d", str(Path.home()))
        elif key == "proxyjump":
            # 支持 user@host:port 形式，只取主机部分用于匹配别名
            target = value.split(",")[0].strip()
            target = target.rsplit("@", 1)[-1]
            current["proxyjump"] = target.split(":")[0]

    flush()

    return ImportResult(hosts=hosts, skipped=skipped, warnings=warnings, proxy_jumps=proxy_jumps)


def resolve_proxy_jumps(result: "ImportResult", store_hosts: list[Host]) -> int:
    """把导入结果里的 ProxyJump 别名解析成主机 id，返回成功解析的数量。

    ``store_hosts`` 是已经保存进 HostStore 的主机（调用方按导入顺序追加）。
    """

    if not result.proxy_jumps:
        return 0

    by_label = {host.label: host for host in store_hosts if host.label}
    by_hostname = {host.hostname: host for host in store_hosts if host.hostname}
    resolved = 0

    for host in store_hosts:
        target = result.proxy_jumps.get(host.label)

        if not target or host.jump_host_id:
            continue

        jump_host = by_label.get(target) or by_hostname.get(target)

        if jump_host is not None and jump_host.id != host.id:
            host.jump_host_id = jump_host.id
            resolved += 1

    return resolved


def export_openssh_config(hosts: list[Host], include_credentials: bool = False) -> str:
    """导出为 ssh config 文本。"""

    lines = ["# 由 PySSH Manager 导出", ""]

    for host in hosts:
        lines.append(f"Host {host.display_name.replace(' ', '_')}")

        if host.hostname:
            lines.append(f"    HostName {host.hostname}")

        if host.port and host.port != 22:
            lines.append(f"    Port {host.port}")

        if host.username:
            lines.append(f"    User {host.username}")

        if host.key_path:
            lines.append(f"    IdentityFile {host.key_path}")

        if host.tags:
            lines.append(f"    # Tags: {', '.join(str(tag) for tag in host.tags)}")

        if not host.verify_host_key:
            lines.append("    StrictHostKeyChecking no")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------
# JSON
# ----------------------------------------------------------------------

def export_json(
    hosts: list[Host],
    credentials: CredentialStore | None = None,
    include_credentials: bool = False,
) -> str:
    """导出为 JSON。``include_credentials`` 为真时附上加密后的凭据。"""

    payload = {
        "version": EXPORT_VERSION,
        "exported_by": "PySSH Manager",
        "hosts": [host.to_dict() for host in hosts],
    }

    if include_credentials and credentials is not None:
        secrets: dict[str, str] = {}

        for host in hosts:
            secret = credentials.get(host.id)

            if secret:
                # 用同样的加密存储逻辑再包一层，避免明文落盘
                secrets[host.id] = _encrypt_for_export(secret)

        payload["credentials"] = secrets

    return json.dumps(payload, ensure_ascii=False, indent=2)


def import_json(text: str) -> ImportResult:
    """从 JSON 导入主机（凭据单独返回给调用方处理）。"""

    hosts: list[Host] = []
    skipped: list[str] = []
    warnings: list[str] = []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return ImportResult(hosts=[], skipped=[], warnings=[f"JSON 解析失败：{error}"])

    items = payload.get("hosts", payload) if isinstance(payload, dict) else payload

    if not isinstance(items, list):
        return ImportResult(hosts=[], skipped=[], warnings=["JSON 结构不正确：缺少 hosts 数组"])

    for item in items:
        if not isinstance(item, dict):
            skipped.append(str(item)[:40])
            continue

        try:
            host = Host.from_dict(item)
        except TypeError as error:
            warnings.append(f"跳过一条记录：{error}")
            continue

        if not host.hostname:
            skipped.append(host.label or "(无地址)")
            continue

        hosts.append(host)

    return ImportResult(hosts=hosts, skipped=skipped, warnings=warnings)


def import_json_credentials(text: str) -> dict[str, str]:
    """读取 JSON 里附带的凭据（已解出明文，由调用方决定是否写库）。"""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict):
        return {}

    secrets = payload.get("credentials")

    if not isinstance(secrets, dict):
        return {}

    result: dict[str, str] = {}

    for host_id, token in secrets.items():
        try:
            result[host_id] = _decrypt_from_export(token)
        except Exception:
            continue

    return result


# ----------------------------------------------------------------------
# 导出用加密（复用凭据库同一套 DPAPI / Fernet 逻辑）
# ----------------------------------------------------------------------

def _encrypt_for_export(plain: str) -> str:
    store = CredentialStore()
    return store._encrypt(plain)  # noqa: SLF001 - 复用同一套加密实现


def _decrypt_from_export(token: str) -> str:
    store = CredentialStore()
    return store._decrypt(token)  # noqa: SLF001
