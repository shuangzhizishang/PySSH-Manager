"""DeepSeek AI 助手：内嵌在 PySSH Manager 里的运维助手。

模块划分：

- ``config``    配置（模型、地址、行为开关；API Key 交给凭据库加密保存）
- ``client``    DeepSeek（OpenAI 兼容）HTTP + SSE 流式客户端，仅用标准库
- ``prompts``   系统提示词：按发行版/包管理器给出正确命令
- ``commands``  从回复里抽取可执行命令，并做危险命令识别
- ``actions``   让 AI 通过结构化指令操作软件本身（打开 SFTP、分屏…）
- ``context``   采集当前主机的发行版、架构、包管理器、终端输出
- ``worker``    放到 QThread 里跑，避免卡界面
"""

from .config import AiConfig, DEFAULT_BASE_URL, DEFAULT_MODEL, MODEL_CHOICES, env_api_key
from .client import AiError, DeepSeekClient
from .commands import SuggestedCommand, extract_commands, risk_of

__all__ = [
    "AiConfig",
    "AiError",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DeepSeekClient",
    "MODEL_CHOICES",
    "SuggestedCommand",
    "env_api_key",
    "extract_commands",
    "risk_of",
]
