"""AI 助手配置。

API Key 不写进配置文件，而是交给 :class:`~sshmanager.credentials.CredentialStore`
（Windows 上是 DPAPI 加密）保存；配置 JSON 里只有模型、地址这类非敏感项。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import settings

#: 凭据库里存 API Key 用的键名
CREDENTIAL_KEY = "ai:deepseek"

DEFAULT_BASE_URL = "https://api.deepseek.com"

DEFAULT_MODEL = "deepseek-chat"

#: 官方模型（deepseek-chat = V3 系列，deepseek-reasoner = 推理模型）
MODEL_CHOICES = ["deepseek-chat", "deepseek-reasoner"]

#: 环境变量兜底（方便脚本/临时使用，不落盘）
ENV_KEYS = ("PYSSH_AI_API_KEY", "DEEPSEEK_API_KEY")


def config_file() -> Path:
    return settings.data_dir() / "ai_config.json"


def env_api_key() -> str | None:
    """从环境变量取 API Key（没有就返回 None）。"""

    for name in ENV_KEYS:
        value = os.environ.get(name, "").strip()

        if value:
            return value

    return None


@dataclass
class AiConfig:
    """AI 助手行为配置。"""

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout: float = 90.0

    #: 提问时附带最近多少行终端输出（0 = 不附带）
    include_terminal_tail: int = 60

    #: 执行命令后自动把输出回传给 AI 继续分析
    auto_analyze_result: bool = True

    #: 附在系统提示词后面的额外要求（比如"用中文回答，命令尽量用 yum"）
    system_extra: str = ""

    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path | None = None) -> None:
        target = Path(path) if path else config_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "AiConfig":
        target = Path(path) if path else config_file()

        if not target.exists():
            return cls()

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()

        if not isinstance(raw, dict):
            return cls()

        known = {field_name for field_name in cls.__dataclass_fields__}
        values = {key: value for key, value in raw.items() if key in known}

        try:
            return cls(**values)
        except (TypeError, ValueError):
            return cls()

    # ------------------------------------------------------------------

    def normalized_base_url(self) -> str:
        """统一成 ``https://host[/v1]`` 形式（去掉末尾斜杠）。"""

        url = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")

        return url or DEFAULT_BASE_URL
