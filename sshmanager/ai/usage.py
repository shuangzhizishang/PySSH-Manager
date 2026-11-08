"""AI 用量账本：把每次调用的 token、缓存命中、花费累计下来。

数据落在 ``data_dir()/ai_usage.json``，重启后累计值还在；「本次」只是内存里的
会话计数，关窗口即清零。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .. import settings
from .config import DEFAULT_MODEL
from .pricing import CN_TZ, canonical_model, cost_of, format_cny, format_percent, format_tokens

#: 账本版本，日后改结构时用来做迁移
USAGE_VERSION = 1


def usage_file() -> Path:
    return settings.data_dir() / "ai_usage.json"


@dataclass
class TokenUsage:
    """一次 API 调用的 token 用量（OpenAI 兼容字段）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def miss_tokens(self) -> int:
        """未命中缓存的输入 tokens。"""

        return max(0, self.prompt_tokens - self.cached_tokens)

    @property
    def hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0

        return self.cached_tokens / self.prompt_tokens

    def is_empty(self) -> bool:
        return self.prompt_tokens <= 0 and self.completion_tokens <= 0

    def text(self) -> str:
        """``12.3k tok · 命中 78%`` 这种一行摘要。"""

        return (
            f"{format_tokens(self.total_tokens or (self.prompt_tokens + self.completion_tokens))} tok"
            f" · 命中 {format_percent(self.cached_tokens, self.prompt_tokens)}"
        )

    @classmethod
    def from_api(cls, raw) -> "TokenUsage":
        """解析接口返回的 ``usage`` 字典（兼容新旧字段名）。"""

        if not isinstance(raw, dict):
            return cls()

        def num(value) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        prompt = num(raw.get("prompt_tokens"))
        completion = num(raw.get("completion_tokens"))
        total = num(raw.get("total_tokens")) or (prompt + completion)

        cached = num(raw.get("prompt_cache_hit_tokens"))

        if not cached:
            details = raw.get("prompt_tokens_details") or {}

            if isinstance(details, dict):
                cached = num(details.get("cached_tokens"))

        # 未命中字段存在时以它为准，避免两家口径打架
        miss = raw.get("prompt_cache_miss_tokens")

        if cached == 0 and miss is not None and prompt:
            cached = max(0, prompt - num(miss))

        reasoning = 0
        completion_details = raw.get("completion_tokens_details") or {}

        if isinstance(completion_details, dict):
            reasoning = num(completion_details.get("reasoning_tokens"))

        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cached_tokens=min(cached, prompt) if prompt else cached,
            reasoning_tokens=reasoning,
        )


@dataclass
class Stats:
    """累计统计（总量或单个模型）。"""

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_cny: float = 0.0

    def add(self, usage: TokenUsage, cost: float = 0.0) -> None:
        self.requests += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens or (usage.prompt_tokens + usage.completion_tokens)
        self.cached_tokens += usage.cached_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.cost_cny += float(cost or 0.0)

    @property
    def hit_rate(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0

        return self.cached_tokens / self.prompt_tokens

    def line(self) -> str:
        """``38 次 · 1.2M tok · 命中 76% · ¥0.0731``"""

        return (
            f"{self.requests} 次 · {format_tokens(self.total_tokens)} tok"
            f" · 命中 {format_percent(self.cached_tokens, self.prompt_tokens)}"
            f" · {format_cny(self.cost_cny)}"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw) -> "Stats":
        if not isinstance(raw, dict):
            return cls()

        known = {name for name in cls.__dataclass_fields__}
        values = {}

        for key, value in raw.items():
            if key not in known:
                continue

            if key == "cost_cny":
                try:
                    values[key] = float(value)
                except (TypeError, ValueError):
                    values[key] = 0.0
            else:
                try:
                    values[key] = max(0, int(value))
                except (TypeError, ValueError):
                    values[key] = 0

        return cls(**values)


class UsageLedger:
    """累计用量 + 本次会话用量。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else usage_file()

        self.total = Stats()
        self.models: dict[str, Stats] = {}
        self.session = Stats()

        #: 最近一次调用的明细（面板上显示"本次"这一条）
        self.last: TokenUsage | None = None
        self.last_model = ""
        self.last_cost = 0.0
        self.last_peak = False

        self._load()

    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(raw, dict):
            return

        self.total = Stats.from_dict(raw.get("total"))
        models = raw.get("models")

        if isinstance(models, dict):
            self.models = {name: Stats.from_dict(value) for name, value in models.items()}

    def save(self) -> None:
        payload = {
            "version": USAGE_VERSION,
            "total": self.total.to_dict(),
            "models": {name: stats.to_dict() for name, stats in self.models.items()},
            "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
        }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)
        except OSError:
            # 写不了就算了，不能让用量统计影响正常对话
            pass

    # ------------------------------------------------------------------

    def record(
        self,
        model: str,
        usage: TokenUsage,
        cost: float,
        *,
        peak: bool = False,
    ) -> Stats:
        """记一次调用；返回累计统计。"""

        key = canonical_model(model) or model or DEFAULT_MODEL

        self.total.add(usage, cost)
        self.models.setdefault(key, Stats()).add(usage, cost)
        self.session.add(usage, cost)

        self.last = usage
        self.last_model = key
        self.last_cost = float(cost or 0.0)
        self.last_peak = bool(peak)

        self.save()

        return self.total

    def estimate(self, model: str, usage: TokenUsage, now=None) -> float:
        """按当前时段价格算这次的花费。"""

        return cost_of(
            usage.prompt_tokens,
            usage.cached_tokens,
            usage.completion_tokens,
            model,
            now=now,
            prices=None,
        )

    def reset(self) -> None:
        """清零累计（本次会话也清零）。"""

        self.total = Stats()
        self.models = {}
        self.session = Stats()
        self.last = None
        self.last_model = ""
        self.last_cost = 0.0
        self.save()

    # ------------------------------------------------------------------

    def session_line(self) -> str:
        """面板顶部的一句话总览。"""

        if self.session.requests <= 0:
            return "本次还没用过 AI"

        last = self.last

        if last is not None and not last.is_empty():
            current = f"本次 {last.text()} · {format_cny(self.last_cost)}"
        else:
            current = f"本次 {self.session.line()}"

        return f"{current} ｜ 累计 {format_cny(self.total.cost_cny)}"

    def rows(self) -> list[tuple[str, Stats]]:
        """按花费从高到低排的模型明细。"""

        return sorted(self.models.items(), key=lambda item: item[1].cost_cny, reverse=True)
