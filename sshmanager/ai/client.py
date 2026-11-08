"""DeepSeek API 客户端（OpenAI 兼容接口，只用标准库）。

支持：

- ``stream=True``：SSE 流式输出，逐块产出，界面能边生成边显示
- ``stream=False``：一次性返回（用于"测试连接"）
- 推理模型（``deepseek-reasoner``）的 ``reasoning_content`` 单独产出
- 常见错误（401 密钥、402 余额、429 限流、5xx）转成中文提示
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator

from .config import AiConfig, DEFAULT_BASE_URL, DEFAULT_MODEL


class AiError(Exception):
    """调用 AI 接口失败（消息可直接展示给用户）。"""


@dataclass
class StreamEvent:
    """流式输出的一个事件：正文增量、推理增量、用量或结束。"""

    kind: str  #: "text" / "reasoning" / "usage" / "done"
    text: str = ""
    usage: dict | None = None


def _error_message(status: int, body: str) -> str:
    brief = (body or "").strip().replace("\n", " ")[:300]

    mapping = {
        400: "请求格式有误（模型名或参数不对？）",
        401: "API Key 无效或已过期，请在『AI 设置』里重新填写",
        402: "账户余额不足，请充值后再试",
        403: "没有权限访问该模型",
        404: "接口地址不对（检查 Base URL，是否需要带 /v1）",
        429: "请求太频繁或额度用尽，稍后再试",
        500: "DeepSeek 服务端错误，稍后重试",
        503: "DeepSeek 服务繁忙，稍后重试",
    }

    reason = mapping.get(status, f"HTTP {status}")

    return f"{reason}：{brief}" if brief else reason


class DeepSeekClient:
    """最小可用的 DeepSeek 客户端。"""

    def __init__(self, api_key: str, config: AiConfig | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self.config = config or AiConfig()

    # ------------------------------------------------------------------
    # 地址
    # ------------------------------------------------------------------

    def endpoint(self) -> str:
        base = self.config.normalized_base_url()

        if base.endswith("/chat/completions"):
            return base

        if base.endswith("/v1"):
            return base + "/chat/completions"

        # 官方地址支持直接拼 /chat/completions；第三方代理一般也兼容 /v1
        if "deepseek.com" in base:
            return base + "/chat/completions"

        return base + "/v1/chat/completions"

    def _api_root(self) -> str:
        """接口根地址：``.../chat/completions`` 去掉后剩下的部分。"""

        endpoint = self.endpoint()

        if endpoint.endswith("/chat/completions"):
            return endpoint[: -len("/chat/completions")]

        return endpoint

    def models_endpoint(self) -> str:
        """``GET /models``：拿官方模型列表（含上下文长度、思考强度档位）。"""

        return self._api_root() + "/models"

    def balance_endpoint(self) -> str:
        """``GET /user/balance``：查账户余额。"""

        return self._api_root() + "/user/balance"

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    def _request(self, messages: list[dict], stream: bool, override: dict | None = None) -> urllib.request.Request:
        if not self.api_key:
            raise AiError("还没有配置 API Key：请打开『AI 设置』填写，或设置环境变量 PYSSH_AI_API_KEY")

        payload: dict = {
            "model": self.config.model or DEFAULT_MODEL,
            "messages": messages,
            "stream": stream,
            "max_tokens": int(self.config.max_tokens),
        }

        # 思考模式默认开启；effort=off 时明确关闭（关闭后 temperature 才生效）
        if self.config.thinking_enabled:
            payload["reasoning_effort"] = self.config.effort
            payload["thinking"] = {"type": "enabled"}
        else:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = float(self.config.temperature)

        if stream:
            # 不加这个拿不到 token 用量，也就没法算花费
            payload["stream_options"] = {"include_usage": True}

        if override:
            payload.update(override)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        return urllib.request.Request(
            self.endpoint(),
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if stream else "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "PySSH-Manager-AI/1.0",
            },
        )

    # ------------------------------------------------------------------

    def chat(self, messages: list[dict], stream: bool = True) -> Iterator[StreamEvent]:
        """发一轮对话。``stream=True`` 时逐块产出。"""

        request = self._request(messages, stream=stream)

        try:
            response = urllib.request.urlopen(request, timeout=float(self.config.timeout))
        except urllib.error.HTTPError as error:
            body = ""

            try:
                body = error.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass

            raise AiError(_error_message(error.code, body)) from error
        except urllib.error.URLError as error:
            raise AiError(f"连不上 AI 服务：{error.reason}") from error
        except socket.timeout as error:
            raise AiError("请求超时：检查网络或代理设置") from error

        if not stream:
            try:
                raw = response.read().decode("utf-8", "replace")
            finally:
                response.close()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as error:
                raise AiError(f"返回内容无法解析：{raw[:200]}") from error

            choices = data.get("choices") or []
            text = ""

            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""

            yield StreamEvent("text", text)
            yield StreamEvent("done", usage=data.get("usage"))
            return

        try:
            for event in self._iter_sse(response):
                yield event
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------

    def _iter_sse(self, response) -> Iterator[StreamEvent]:
        """解析 SSE：``data: {json}`` 直到 ``[DONE]``。"""

        finished = False

        while not finished:
            raw_line = response.readline()

            if not raw_line:
                break

            line = raw_line.decode("utf-8", "replace").strip()

            if not line or line.startswith(":"):
                continue

            if not line.startswith("data:"):
                continue

            data = line[5:].strip()

            if data == "[DONE]":
                finished = True
                break

            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue

            # 有些代理会在流里塞错误
            if payload.get("error"):
                message = payload["error"].get("message") if isinstance(payload["error"], dict) else str(payload["error"])
                raise AiError(f"AI 服务返回错误：{message}")

            if payload.get("usage"):
                yield StreamEvent("usage", usage=payload["usage"])

            choices = payload.get("choices") or []

            if not choices:
                continue

            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content") or ""

            if reasoning:
                yield StreamEvent("reasoning", reasoning)

            content = delta.get("content") or ""

            if content:
                yield StreamEvent("text", content)

        yield StreamEvent("done")

    # ------------------------------------------------------------------

    def test_connection(self) -> str:
        """发一条极短的请求验证 Key/地址/模型，返回模型回答（或空串）。"""

        events = self.chat([{"role": "user", "content": "回复 ok，不要多说"}], stream=False)
        text = ""

        for event in events:
            if event.kind == "text":
                text = event.text

        return (text or "").strip()

    # ------------------------------------------------------------------

    def complete(self, messages: list[dict]) -> str:
        """非流式取完整回复（供内部/测试使用）。"""

        return "".join(
            event.text for event in self.chat(messages, stream=True) if event.kind == "text"
        )

    # ------------------------------------------------------------------
    # 模型列表 / 余额
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict:
        if not self.api_key:
            raise AiError("还没有配置 API Key：请打开『AI 设置』填写，或设置环境变量 PYSSH_AI_API_KEY")

        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "PySSH-Manager-AI/1.0",
            },
        )

        try:
            response = urllib.request.urlopen(request, timeout=float(self.config.timeout))
        except urllib.error.HTTPError as error:
            body = ""

            try:
                body = error.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass

            raise AiError(_error_message(error.code, body)) from error
        except urllib.error.URLError as error:
            raise AiError(f"连不上 AI 服务：{error.reason}") from error
        except socket.timeout as error:
            raise AiError("请求超时：检查网络或代理设置") from error

        try:
            raw = response.read().decode("utf-8", "replace")
        finally:
            response.close()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AiError(f"返回内容无法解析：{raw[:200]}") from error

        if not isinstance(data, dict):
            raise AiError(f"返回内容格式不对：{raw[:200]}")

        if data.get("error"):
            payload = data["error"]
            message = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise AiError(f"AI 服务返回错误：{message}")

        return data

    def list_models(self) -> list[dict]:
        """``GET /models``：返回官方模型列表（每项含 id/name/上下文长度/思考强度）。"""

        data = self._get_json(self.models_endpoint())
        raw_items = data.get("data")

        if not isinstance(raw_items, list):
            return []

        models: list[dict] = []

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            model_id = str(item.get("id") or "").strip()

            if not model_id:
                continue

            effort = item.get("effort") or {}
            levels = effort.get("supported_levels") if isinstance(effort, dict) else None

            models.append(
                {
                    "id": model_id,
                    "name": str(item.get("name") or model_id),
                    "context_window": item.get("context_window"),
                    "max_output_tokens": item.get("max_output_tokens"),
                    "effort_levels": list(levels) if isinstance(levels, list) else ["low", "high", "max"],
                    "default_effort": str(effort.get("default_level") or "high")
                    if isinstance(effort, dict)
                    else "high",
                }
            )

        return models

    def balance(self) -> dict:
        """``GET /user/balance``：返回 ``{"is_available", "infos": [{currency, total, granted, topped_up}]}``。"""

        data = self._get_json(self.balance_endpoint())
        raw_infos = data.get("balance_infos")

        infos: list[dict] = []

        if isinstance(raw_infos, list):
            for item in raw_infos:
                if not isinstance(item, dict):
                    continue

                infos.append(
                    {
                        "currency": str(item.get("currency") or "CNY"),
                        "total": str(item.get("total_balance") or "0"),
                        "granted": str(item.get("granted_balance") or "0"),
                        "topped_up": str(item.get("topped_up_balance") or "0"),
                    }
                )

        return {"is_available": bool(data.get("is_available")), "infos": infos}

