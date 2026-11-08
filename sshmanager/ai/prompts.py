"""系统提示词与上下文拼装。

目标是让 AI 在不同发行版上给出**正确**的命令：先判断发行版与包管理器，
再决定用 yum / dnf / apt / zypper / apk / pacman，而不是一律给 ``apt``。
"""

from __future__ import annotations

from .config import AiConfig

#: 常见发行版 → 包管理器 / 注意事项
DISTRO_PLAYBOOK: dict[str, str] = {
    "centos": "CentOS 7（老系统）：包管理用 yum，python 可能是 python2，服务用 systemctl；"
    "官方源已 EOL，必要时可用 aliyun vault 源",
    "rhel": "RHEL：用 yum/dnf；注意 subscription 限制",
    "red hat": "RHEL：用 yum/dnf",
    "rocky": "Rocky Linux：用 dnf",
    "almalinux": "AlmaLinux：用 dnf",
    "fedora": "Fedora：用 dnf",
    "amazon": "Amazon Linux：用 yum/dnf（视版本）",
    "openeuler": "openEuler：用 dnf/yum",
    "ubuntu": "Ubuntu：用 apt-get/apt，必要时 apt update；服务用 systemctl",
    "debian": "Debian：用 apt-get/apt；服务用 systemctl",
    "kali": "Kali：apt；注意默认不装 ssh 服务",
    "linux mint": "Linux Mint：apt",
    "opensuse": "openSUSE/SLES：用 zypper",
    "sles": "SLES：用 zypper",
    "alpine": "Alpine：用 apk，shell 是 busybox ash，服务用 rc-service/openrc（不是 systemctl）",
    "arch": "Arch：用 pacman -Syu，服务用 systemctl",
    "manjaro": "Manjaro：pacman",
    "gentoo": "Gentoo：emerge",
    "freebsd": "FreeBSD：pkg install，服务用 service(8)",
}

OUTPUT_RULES = """\
输出要求：
1. 用中文回答，简洁直接：先给结论/判断，再给命令，最后必要时补一两句说明。
2. 所有要执行的命令必须放在 ```bash 代码块里，**一条命令一个块**（多行脚本可以合成一个块）。
   不要在代码块里写 "$ " 或 "# " 提示符。
3. 你**不能直接执行命令**。用户点击"执行"后，软件会把真实输出回传给你，届时再基于输出继续分析。
   绝对不要编造命令输出。
4. 破坏性或有风险的操作（删数据、格式化、关机、清空防火墙等）必须先用一句话提醒风险，
   并尽量给出更安全的替代方案（比如先备份、先 df -h 看情况）。
5. 需要更多信息才能判断时，只给出一条诊断命令，并说明你想看什么。
6. 不同发行版命令不同：先看下面"环境"里的发行版与包管理器，不要给错包管理器。
7. 如果用户想操作软件界面（打开 SFTP、分屏、开新会话、开关同步输入等），
   用 ```pyssh-action 代码块输出一行 JSON，例如：{"action": "open_sftp"}。
"""

ACTION_CATALOG = """\
可用的软件操作（pyssh-action 的 action 字段）：
- open_sftp：为当前主机会话打开 SFTP 文件面板
- open_forwards：打开端口转发对话框
- split_horizontal / split_vertical：左右 / 上下分屏（同一主机再开一个会话）
- new_session：给当前主机开一条新的并行会话
- reconnect：重连当前会话
- sync_input_on / sync_input_off：开启/关闭"同步输入到所有会话"
- copy_output：复制当前终端全部输出
- save_output：把当前终端输出另存为文件
"""


def build_system_prompt(config: AiConfig | None = None, environment: dict | None = None) -> str:
    """拼出系统提示词（带当前机器环境与发行版要点）。"""

    config = config or AiConfig()
    parts = [
        "你是 PySSH Manager 内置的 Linux 运维助手，帮助用户通过 SSH 管理各种 Linux 服务器。"
        "你熟悉 CentOS/RHEL、Ubuntu/Debian、openSUSE、Alpine、Arch 等发行版的差异。",
        "工作方式：用户提出需求 → 你判断问题 → 给出可执行命令 → 用户执行后把输出回传给你 → 你继续分析。",
        OUTPUT_RULES,
        ACTION_CATALOG,
    ]

    if environment:
        parts.append(_environment_section(environment))

    if (config.system_extra or "").strip():
        parts.append("用户额外要求：\n" + config.system_extra.strip())

    return "\n\n".join(parts)


def _environment_section(environment: dict) -> str:
    lines = ["当前环境："]

    def add(label: str, key: str, fallback: str = "未知"):
        value = (environment.get(key) or "").strip()

        if value:
            lines.append(f"- {label}：{value}")

    add("主机", "host")
    add("登录用户", "user")
    add("发行版", "pretty_name")
    add("内核/架构", "uname")
    add("Shell", "shell")
    add("包管理器", "package_managers")
    add("是否 root", "is_root")
    add("备注", "note")

    distro_key = (environment.get("distro_id") or "").lower()
    playbook = DISTRO_PLAYBOOK.get(distro_key)

    if playbook is None:
        pretty = (environment.get("pretty_name") or "").lower()

        for name, hint in DISTRO_PLAYBOOK.items():
            if name.lower() in pretty:
                playbook = hint
                break

    if playbook:
        lines.append(f"- 发行版要点：{playbook}")

    return "\n".join(lines)


def build_context_message(environment: dict | None, terminal_tail: str = "") -> str:
    """把环境与终端输出拼成一条上下文（作为 user 消息前缀，避免污染对话历史）。"""

    blocks: list[str] = []

    if environment:
        blocks.append(_environment_section(environment))

    if terminal_tail.strip():
        blocks.append(
            "当前终端最近的输出（仅供参考，可能包含旧的无关内容）：\n"
            "```text\n" + terminal_tail.strip()[-6000:] + "\n```"
        )

    if not blocks:
        return ""

    return "\n\n".join(blocks) + "\n\n---\n\n"
