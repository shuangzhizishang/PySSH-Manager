"""从 AI 回复里抽取可执行命令，并识别危险操作。

约定（写在系统提示词里）：助手把要执行的命令放在 ```bash 代码块中，
一条命令一个块。界面把每个块渲染成一张"建议命令"卡片，由用户确认后执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ```lang\n ... ```（lang 可为空）
BLOCK_PATTERN = re.compile(r"```(?P<lang>[A-Za-z0-9_+\-]*)[ \t]*\n(?P<code>.*?)```", re.S)

#: 这些语言的代码块不是命令（应用操作块由 actions.py 单独解析）
SKIP_LANGS = {
    "python",
    "python3",
    "py",
    "json",
    "yaml",
    "yml",
    "toml",
    "ini",
    "conf",
    "nginx",
    "text",
    "diff",
    "sql",
    "html",
    "javascript",
    "js",
    "powershell",
    "ps1",
    "pyssh-action",
}

#: 明显破坏性的命令
DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"rm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?![\w.])", "递归删除根目录内容"),
    (r"rm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/\s*$", "递归删除根目录"),
    (r"rm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/\*", "删除根目录下所有文件"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\b[^\n]*\bof=/dev/", "直接写块设备"),
    (r">\s*/dev/(sd|nvme|hd|vd|xvd)", "覆盖磁盘设备"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启，会导致连接断开"),
    (r"\binit\s+0\b", "关机"),
    (r"\binit\s+6\b", "重启"),
    (r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:", "fork 炸弹"),
    (r"\bchmod\s+-R\s+777\s+/(?![\w.])", "把整个系统权限放开"),
    (r"\bchown\s+-R\s+\S+\s+/(?![\w.])", "改整个系统的所有者"),
    (r"\bdd\b[^\n]*\bif=/dev/zero\b", "用零覆盖磁盘"),
    (r"\b(shred|wipefs)\b", "擦除数据"),
    (r"\b(iptables|nft)\s+-F\b", "清空防火墙规则"),
    (r"\bhistory\s+-c\b", "清空历史记录"),
]

#: 需要谨慎、但常见的操作
CAREFUL_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\b", "删除文件/目录"),
    (r"\b(yum|dnf|apt|apt-get|zypper)\s+(remove|erase|autoremove|purge)\b", "卸载软件包"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "停止/禁用系统服务"),
    (r"\bservice\s+\S+\s+stop\b", "停止服务"),
    (r"\bkill\s+-9\b", "强制结束进程"),
    (r"\bchmod\b", "修改权限"),
    (r"\bchown\b", "修改所有者"),
    (r"\buserdel\b|\bgroupdel\b", "删除用户/组"),
    (r"\bpasswd\b", "修改密码"),
    (r"\bmv\s+[^\n]*\s+/(?![\w.])", "移动文件到系统目录"),
    (r">\s*/etc/", "覆盖 /etc 下的配置文件"),
    (r"\btruncate\b|\b>:?\s*\S+", "清空文件内容"),
    (r"\b(nc|netcat|socat)\b[^\n]*\b-e\b", "可能开反向 shell"),
]

RISK_LABELS = {"safe": "可直接执行", "careful": "需谨慎", "danger": "危险操作"}


@dataclass
class SuggestedCommand:
    """一条建议命令。"""

    code: str
    title: str = ""
    risk: str = "safe"
    reason: str = ""

    @property
    def risk_label(self) -> str:
        return RISK_LABELS.get(self.risk, self.risk)


#: 行首提示符：``$ `` / ``> `` / ``[root@host ~]# ``。
#: 注意**不能**剥掉纯粹的 ``# 注释``——那也是合法的 shell 注释。
_PROMPT_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\][#$>]?|[$>])\s+")


def clean_code(code: str) -> str:
    """去掉 Markdown 里常见的提示符（``$ `` / ``[root@x]# ``）和首尾空行。"""

    lines = []

    for line in (code or "").replace("\r\n", "\n").split("\n"):
        stripped = _PROMPT_PREFIX.sub("", line)
        lines.append(stripped.rstrip())

    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def _title_of(code: str) -> str:
    """取命令说明：优先首行注释，其次命令本身。"""

    lines = [line.strip() for line in code.split("\n") if line.strip()]

    if not lines:
        return "命令"

    for line in lines[:2]:
        if line.startswith("#") and len(line) > 1:
            return line.lstrip("#").strip()[:80]

    first = lines[0]
    # 去掉 sudo 这类前缀，标题更好看
    first = re.sub(r"^sudo\s+", "", first)

    return first[:80]


def risk_of(code: str) -> tuple[str, str]:
    """判断风险等级，返回 ``(level, reason)``。"""

    text = code or ""

    for pattern, reason in DANGER_PATTERNS:
        if re.search(pattern, text):
            return "danger", reason

    # sudo 自带授权，本身不算危险；但 sudo 后面的操作已经按上面的规则判断
    for pattern, reason in CAREFUL_PATTERNS:
        if re.search(pattern, text):
            return "careful", reason

    return "safe", ""


def insert_plan(code: str) -> tuple[str, int]:
    """「插入终端」的拆分方案，返回 ``(要直接插入的第一行, 其余行数)``。

    终端把换行当回车，多行文本逐行插入会被立刻执行（危险命令就绕过了确认框），
    所以多行只插第一行，其余行交给调用方放剪贴板。
    """

    lines = [line.rstrip() for line in (code or "").splitlines() if line.strip()]

    if not lines:
        return "", 0

    if len(lines) == 1:
        return lines[0], 0

    return lines[0], len(lines) - 1


def extract_commands(text: str) -> list[SuggestedCommand]:
    """从回复里抽取所有命令块（保持顺序、去重）。"""
    result: list[SuggestedCommand] = []
    seen: set[str] = set()

    for match in BLOCK_PATTERN.finditer(text or ""):
        lang = (match.group("lang") or "").strip().lower()

        if lang in SKIP_LANGS:
            continue

        code = clean_code(match.group("code"))

        if not code or code in seen:
            continue

        seen.add(code)
        risk, reason = risk_of(code)
        result.append(SuggestedCommand(code=code, title=_title_of(code), risk=risk, reason=reason))

    return result


def strip_code_blocks(text: str) -> str:
    """把代码块替换成占位符，方便界面先显示说明文字。"""

    return BLOCK_PATTERN.sub(lambda match: "\n\n（下方有可执行命令）\n\n", text or "")
