"""采集当前主机的环境信息（发行版、架构、Shell、包管理器）。

走 ``SSHClient.exec_command`` 的**独立通道**，不打扰用户的交互式终端，
也不会在屏幕上留下痕跡。
"""

from __future__ import annotations

import re

#: 一次采集：发行版 + 架构 + shell + 可用的包管理器 + 是否 root + 主机名
ENV_COMMAND = (
    "echo '###OS'; cat /etc/os-release 2>/dev/null | head -12; "
    "echo '###KERNEL'; uname -srm 2>/dev/null; "
    "echo '###HOST'; hostname 2>/dev/null; "
    "echo '###SHELL'; echo \"$SHELL\"; "
    "echo '###USER'; id -un 2>/dev/null; id -u 2>/dev/null; "
    "echo '###PM'; for pm in yum dnf apt-get apt zypper apk pacman emerge pkg; do "
    "command -v $pm >/dev/null 2>&1 && printf '%s ' $pm; done; echo; "
    "echo '###VIRT'; (systemd-detect-virt 2>/dev/null || echo unknown) | head -1"
)

#: 只读命令，用于一键诊断
DIAGNOSE_COMMAND = (
    "echo '###OS'; cat /etc/os-release 2>/dev/null | head -6; "
    "echo '###DISK'; df -hT 2>/dev/null | head -12; "
    "echo '###MEM'; free -m 2>/dev/null; "
    "echo '###LOAD'; uptime 2>/dev/null; "
    "echo '###SVC'; (systemctl --failed --no-pager 2>/dev/null | head -15 || true)"
)


def _section(text: str, name: str) -> str:
    """取出 ``###NAME`` 到下一个 ``###`` 之间的内容。"""

    pattern = re.compile(r"###" + re.escape(name) + r"\s*\n?(.*?)(?=\n###|\Z)", re.S)
    match = pattern.search(text or "")

    return (match.group(1) if match else "").strip()


def parse_os_release(text: str) -> dict[str, str]:
    """解析 ``/etc/os-release`` 的 KEY=VALUE 形式。"""

    result: dict[str, str] = {}

    for line in (text or "").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        result[key.strip()] = value

    return result


def parse_environment_output(text: str) -> dict[str, str]:
    """把 :data:`ENV_COMMAND` 的输出解析成环境字典。"""

    os_release = parse_os_release(_section(text, "OS"))
    kernel = _section(text, "KERNEL")
    shell = _section(text, "SHELL")
    user_line = _section(text, "USER").splitlines()
    packages = _section(text, "PM")
    hostname = _section(text, "HOST")
    virt = _section(text, "VIRT")

    environment: dict[str, str] = {
        "pretty_name": os_release.get("PRETTY_NAME", "") or os_release.get("NAME", ""),
        "distro_id": (os_release.get("ID", "") or "").lower(),
        "uname": kernel,
        "shell": shell,
        "host": hostname,
        "user": user_line[0].strip() if user_line else "",
        "package_managers": packages,
        "kernel": kernel,
    }

    if len(user_line) > 1 and user_line[1].strip() == "0":
        environment["is_root"] = "是（root）"

    if virt and virt.lower() != "unknown":
        environment["note"] = f"虚拟化：{virt}"

    return {key: value for key, value in environment.items() if value}


def capture_environment(client, timeout: float = 15.0) -> dict[str, str]:
    """通过独立通道采集环境；失败返回空字典（不影响对话继续）。"""

    if client is None:
        return {}

    try:
        stdin, stdout, stderr = client.exec_command(ENV_COMMAND, timeout=timeout)
        raw = stdout.read() + stderr.read()

        for stream in (stdin, stdout, stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        return parse_environment_output(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def capture_command_output(client, command: str, timeout: float = 20.0) -> str:
    """在独立通道上跑一条只读命令，返回合并后的文本输出。"""

    if client is None:
        return ""

    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        raw = stdout.read() + stderr.read()

        for stream in (stdin, stdout, stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

        return raw.decode("utf-8", "replace").strip()
    except Exception as error:  # noqa: BLE001
        return f"（采集失败：{error}）"


def terminal_tail(view, lines: int = 60) -> str:
    """取终端最近 ``lines`` 行文本（含回滚区）。"""

    if view is None or lines <= 0:
        return ""

    try:
        text = view.output_text()
    except Exception:  # noqa: BLE001
        return ""

    rows = text.splitlines()

    return "\n".join(rows[-lines:]) if rows else ""
