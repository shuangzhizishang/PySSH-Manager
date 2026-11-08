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
            "temperature": float(self.config.temperature),
            "max_tokens": int(self.config.max_tokens),
        }

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

            choices = payload.get("choices") or []

            if not choices:
                if payload.get("usage"):
                    yield StreamEvent("usage", usage=payload["usage"])

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
