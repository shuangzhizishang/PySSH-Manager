"""DeepSeek 计费：按官方价目表估算一次对话花了多少钱。

价格单位是「元 / 百万 tokens」，分两档：

- **高峰**：北京时间周一至周五（不含法定节假日）9:00–12:00、14:00–18:00
- **空闲**：其余时间（含周末全天），价格是高峰的一半

法定节假日无法离线判断，节假日按工作日估算会略微高估，误差很小。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: 北京时间（UTC+8，不用 zoneinfo，避免 Windows 上缺 tzdata）
CN_TZ = timezone(timedelta(hours=8))

#: 高峰时段（北京时间，左闭右开）
PEAK_WINDOWS = ((9, 12), (14, 18))


@dataclass(frozen=True)
class ModelPrice:
    """一个模型在某个时段的单价（元 / 百万 tokens）。"""

    hit: float = 0.0  #: 输入·缓存命中
    miss: float = 0.0  #: 输入·缓存未命中
    output: float = 0.0  #: 输出（含思维链）

    def to_dict(self) -> dict:
        return {"hit": self.hit, "miss": self.miss, "output": self.output}

    @classmethod
    def from_dict(cls, raw) -> "ModelPrice":
        if isinstance(raw, ModelPrice):
            return raw

        data = raw if isinstance(raw, dict) else {}

        def num(key: str, fallback: float) -> float:
            try:
                return float(data.get(key, fallback))
            except (TypeError, ValueError):
                return fallback

        return cls(hit=num("hit", 0.0), miss=num("miss", 0.0), output=num("output", 0.0))


#: 官方价目表（2026-10-17 起）：元 / 百万 tokens
DEFAULT_PRICES: dict[str, dict[str, ModelPrice]] = {
    "deepseek-flash": {
        "idle": ModelPrice(hit=0.02, miss=1.0, output=4.0),
        "peak": ModelPrice(hit=0.04, miss=2.0, output=8.0),
    },
    "deepseek-v4-pro": {
        "idle": ModelPrice(hit=0.15, miss=4.5, output=13.5),
        "peak": ModelPrice(hit=0.30, miss=9.0, output=27.0),
    },
}

#: 旧模型名/别名 → 实际计费的模型
MODEL_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
    "deepseek-v4-pro-0813": "deepseek-v4-pro",
}

#: 模型显示名
MODEL_LABELS = {
    "deepseek-flash": "DeepSeek-V4.1-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
}


def canonical_model(model: str | None) -> str:
    """把旧名/别名统一成计费用的模型 id。"""

    name = (model or "").strip()

    return MODEL_ALIASES.get(name, name)


def model_label(model: str | None) -> str:
    """给界面显示的模型名（未知模型原样返回）。"""

    name = canonical_model(model)

    return MODEL_LABELS.get(name, (model or "").strip())


def default_prices() -> dict[str, dict[str, ModelPrice]]:
    """返回一份可随意修改的默认价目表副本。"""

    return copy.deepcopy(DEFAULT_PRICES)


def merged_prices(overrides: dict | None) -> dict[str, dict[str, ModelPrice]]:
    """默认价目表 + 用户在设置里改过的价格。"""

    prices = default_prices()

    if not isinstance(overrides, dict):
        return prices

    for model, bands in overrides.items():
        if not isinstance(bands, dict):
            continue

        target = prices.setdefault(model, {"idle": ModelPrice(), "peak": ModelPrice()})

        for band in ("idle", "peak"):
            if band in bands:
                target[band] = ModelPrice.from_dict(bands[band])

    return prices


def beijing_now(now: datetime | None = None) -> datetime:
    """取北京时间；``now`` 可注入（测试用）。"""

    if now is None:
        return datetime.now(CN_TZ)

    if now.tzinfo is None:
        return now.replace(tzinfo=CN_TZ)

    return now.astimezone(CN_TZ)


def is_peak(now: datetime | None = None) -> bool:
    """当前是否处于高峰时段（北京时间工作日 9-12、14-18）。"""

    moment = beijing_now(now)

    # Monday = 0
    if moment.weekday() >= 5:
        return False

    for start, end in PEAK_WINDOWS:
        if start <= moment.hour < end:
            return True

    return False


def band_name(now: datetime | None = None) -> str:
    return "peak" if is_peak(now) else "idle"


def peak_note(now: datetime | None = None) -> str:
    """一句话说明现在按哪档计价（给界面用）。"""

    if is_peak(now):
        return "当前为高峰时段（北京时间 工作日 9:00–12:00、14:00–18:00）"

    return "当前为空闲时段（高峰价的 50%）"


def price_for(
    model: str,
    now: datetime | None = None,
    prices: dict | None = None,
) -> ModelPrice:
    """取某模型当前时段的单价；不认识的模型按 Flash 计。"""

    table = merged_prices(prices)
    entry = table.get(canonical_model(model)) or table.get("deepseek-flash")

    if not entry:
        return ModelPrice()

    return entry.get(band_name(now)) or ModelPrice()


def price_pair(model: str, prices: dict | None = None) -> tuple[ModelPrice, ModelPrice]:
    """返回 ``(空闲价, 高峰价)``。"""

    table = merged_prices(prices)
    entry = table.get(canonical_model(model)) or table.get("deepseek-flash") or {}

    return entry.get("idle", ModelPrice()), entry.get("peak", ModelPrice())


# ----------------------------------------------------------------------
# 金额计算
# ----------------------------------------------------------------------


def cost_of(
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    model: str,
    now: datetime | None = None,
    prices: dict | None = None,
) -> float:
    """算一次调用的花费（元）。``cached_tokens`` 是命中缓存的输入部分。"""

    price = price_for(model, now=now, prices=prices)

    prompt = max(0, int(prompt_tokens or 0))
    cached = min(max(0, int(cached_tokens or 0)), prompt)
    missed = prompt - cached
    output = max(0, int(completion_tokens or 0))

    return (
        cached / 1_000_000 * price.hit
        + missed / 1_000_000 * price.miss
        + output / 1_000_000 * price.output
    )


def format_cny(value: float) -> str:
    """金额格式化：小额多留几位，避免显示成 ¥0.00。"""

    amount = max(0.0, float(value or 0.0))

    if amount == 0:
        return "¥0"

    if amount < 0.0001:
        return f"¥{amount:.6f}".rstrip("0").rstrip(".")

    if amount < 0.01:
        return f"¥{amount:.5f}".rstrip("0").rstrip(".")

    if amount < 1:
        return f"¥{amount:.4f}"

    return f"¥{amount:.2f}"


def format_tokens(count: int) -> str:
    """tokens 数缩写：812 / 1.2k / 3.4M。"""

    value = max(0, int(count or 0))

    if value < 1000:
        return str(value)

    if value < 1_000_000:
        text = f"{value / 1000:.1f}"

        return text.rstrip("0").rstrip(".") + "k"

    text = f"{value / 1_000_000:.2f}"

    return text.rstrip("0").rstrip(".") + "M"


def format_percent(hit: int, total: int) -> str:
    """缓存命中率。"""

    if total <= 0:
        return "0%"

    return f"{max(0, min(100, round(hit * 100 / total)))}%"
