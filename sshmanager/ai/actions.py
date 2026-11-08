"""让 AI 通过结构化指令操作软件本身（pyssh-action）。

模型只能"建议"操作；界面把操作渲染成按钮，用户点了才执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class AppAction:
    """一个可执行的软件操作。"""

    name: str
    label: str
    description: str


ACTIONS: dict[str, AppAction] = {
    "open_sftp": AppAction("open_sftp", "打开 SFTP", "为当前主机会话打开 SFTP 文件面板"),
    "open_forwards": AppAction("open_forwards", "端口转发", "打开端口转发对话框"),
    "split_horizontal": AppAction("split_horizontal", "左右分屏", "左右分屏（同一主机再开一个会话）"),
    "split_vertical": AppAction("split_vertical", "上下分屏", "上下分屏（同一主机再开一个会话）"),
    "new_session": AppAction("new_session", "新会话", "给当前主机开一条新的并行会话"),
    "reconnect": AppAction("reconnect", "重新连接", "重连当前会话"),
    "sync_input_on": AppAction("sync_input_on", "开启同步输入", "把输入同时发送到其它会话"),
    "sync_input_off": AppAction("sync_input_off", "关闭同步输入", "不再向其它会话广播输入"),
    "copy_output": AppAction("copy_output", "复制输出", "复制当前终端全部输出到剪贴板"),
    "save_output": AppAction("save_output", "保存输出", "把当前终端输出另存为文本文件"),
}

#: ```pyssh-action\n{...}\n```
ACTION_BLOCK = re.compile(r"```pyssh-action[ \t]*\n(?P<body>.*?)```", re.S)


@dataclass
class SuggestedAction:
    """一条建议的软件操作。"""

    action: AppAction
    reason: str = ""


def extract_actions(text: str) -> list[SuggestedAction]:
    """从回复里抽取 ``pyssh-action`` 块（去重）。"""

    result: list[SuggestedAction] = []
    seen: set[str] = set()

    for match in ACTION_BLOCK.finditer(text or ""):
        body = (match.group("body") or "").strip()

        if not body:
            continue

        # 允许一行 JSON，或多行对象
        payload: dict = {}

        try:
            parsed = json.loads(body)
            payload = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            payload = {}

        name = str(payload.get("action") or payload.get("name") or "").strip().lower()
        action = ACTIONS.get(name)

        if action is None or name in seen:
            continue

        seen.add(name)
        reason = str(payload.get("reason") or "").strip()[:120]
        result.append(SuggestedAction(action=action, reason=reason))

    return result


def strip_action_blocks(text: str) -> str:
    """把 ``pyssh-action`` 块换成一句提示（界面里已经渲染成操作卡片）。"""

    return ACTION_BLOCK.sub("\n\n（下方有可执行的软件操作）\n\n", text or "")
