from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings, settings


logger = logging.getLogger(__name__)


class ModelClient:
    """Chat、Embedding 和 Rerank 接口客户端。"""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or settings
        self._blocked_until: dict[str, float] = {}
        self.request_attempt_count = 0
        self.retry_count = 0
        self.timeout_event_count = 0
        self.connection_error_event_count = 0
        self.chat_prompt_tokens = 0
        self.chat_completion_tokens = 0
        self.embedding_tokens = 0
        self.rerank_tokens = 0
        self.last_chat_model = ""
        self.json_retry_count = 0

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key)

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        payload = {
            "model": self.config.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
            "max_tokens": self.config.llm_max_tokens,
        }
        thinking_format = self.config.thinking_format
        if thinking_format == "deepseek":
            payload["thinking"] = {
                "type": "enabled" if self.config.llm_enable_thinking else "disabled"
            }
        elif thinking_format == "enable_thinking":
            payload["enable_thinking"] = self.config.llm_enable_thinking
        elif thinking_format != "none":
            raise ValueError(f"不支持的 LLM_THINKING_FORMAT：{thinking_format}")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        result = self._post(self.config.chat_url, payload)
        usage = result.get("usage") or {}
        self.chat_prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.chat_completion_tokens += int(usage.get("completion_tokens") or 0)
        self.last_chat_model = str(result.get("model") or self.config.llm_model)
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("模型输出达到 LLM_MAX_TOKENS 上限，结果已截断")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("大模型返回空 message.content")
            return content.strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("大模型响应缺少 message.content") from exc

    def chat_json(self, system: str, user: str) -> dict[str, Any]:
        raw = self.chat(system, user, json_mode=True)
        try:
            return self._parse_json(raw)
        except RuntimeError:
            # JSON 模式也可能返回格式错误；只重试一次，不使用本地语义兜底。
            self.json_retry_count += 1
            repaired = self.chat(
                system + "\n上次输出不是有效 JSON 对象。请重新输出完整、合法的 JSON 对象，"
                "不要使用 Markdown 围栏或解释文字。保持原请求的业务口径、筛选条件与计算语义。",
                user + "\n上次格式错误的输出：\n" + raw,
                json_mode=True,
            )
            return self._parse_json(repaired)

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            completed = ModelClient._complete_outer_containers(cleaned)
            if completed is not None:
                try:
                    payload = json.loads(completed)
                except json.JSONDecodeError:
                    pass
                else:
                    logger.info("model_json_outer_container_completed")
                    return payload
            match = re.search(r"\{[\s\S]*\}", cleaned)
            if not match:
                raise RuntimeError("大模型未返回有效 JSON")
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise RuntimeError("大模型未返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("大模型返回的 JSON 必须是对象")
        return payload

    @staticmethod
    def _complete_outer_containers(raw: str) -> str | None:
        """Only close outer containers after a complete nested object/array.

        Never infer a value, repair strings/SQL, insert commas or accept token-limit
        truncation (chat rejects it before parsing). Normal contract checks still run.
        """
        if not raw.startswith("{") or not raw.endswith(("}", "]")):
            return None
        stack: list[str] = []
        quoted = escaped = False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                stack.append(char)
            elif char in "}]":
                if not stack or stack.pop() != ("{" if char == "}" else "["):
                    return None
        if quoted or not 1 <= len(stack) <= 2:
            return None
        return raw + "".join("}" if char == "{" else "]" for char in reversed(stack))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 10):
            payload = {
                "model": self.config.embedding_model,
                "input": texts[start : start + 10],
                "dimensions": self.config.embedding_dimensions,
                "encoding_format": "float",
            }
            result = self._post(
                self.config.embeddings_url, payload, api_key=self.config.embedding_key
            )
            usage = result.get("usage") or {}
            self.embedding_tokens += int(usage.get("total_tokens") or 0)
            items = sorted(result.get("data", []), key=lambda item: item.get("index", 0))
            if any(
                len(item["embedding"]) != self.config.embedding_dimensions
                for item in items
            ):
                raise RuntimeError("Embedding 返回维度与 EMBEDDING_DIMENSIONS 不一致")
            vectors.extend(self._normalize(item["embedding"]) for item in items)
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding 返回数量与输入不一致")
        return vectors

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if not documents:
            return []
        payload = {
            "model": self.config.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "instruct": "Rank database schema fields by relevance to the user's analytics query.",
        }
        result = self._post(
            self.config.rerank_url, payload, api_key=self.config.rerank_key
        )
        self.rerank_tokens += int((result.get("usage") or {}).get("total_tokens") or 0)
        output = [
            (int(item["index"]), float(item.get("relevance_score", item.get("score", 0))))
            for item in result.get("results", [])
        ]
        return sorted(output, key=lambda item: item[1], reverse=True)

    def _post(
        self, url: str, payload: dict[str, Any], *, api_key: str | None = None
    ) -> dict[str, Any]:
        from .runtime.execution import durable_call
        return durable_call("model", {"url": url, "payload": payload}, lambda: self._post_uncached(url, payload, api_key=api_key))

    def _post_uncached(
        self, url: str, payload: dict[str, Any], *, api_key: str | None = None
    ) -> dict[str, Any]:
        from .runtime.execution import CURRENT
        execution = CURRENT.get()
        credential = self.config.api_key if api_key is None else api_key
        if not credential:
            raise RuntimeError("当前模型接口的 API Key 尚未配置")
        if time.monotonic() < self._blocked_until.get(url, 0):
            raise RuntimeError("模型接口暂时处于30秒熔断窗口，请稍后重试")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            timeout = self.config.timeout
            if execution:
                execution.attempt()
                timeout = min(timeout, execution.check())
            self.request_attempt_count += 1
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                for secret in {self.config.api_key, self.config.embedding_key, self.config.rerank_key}:
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                detail = detail[:500]
                last_error = RuntimeError(f"模型接口返回 HTTP {exc.code}: {detail}")
                if exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                error_text = str(exc).lower()
                if any(token in error_text for token in ("timed out", "timeout", "超时")):
                    self.timeout_event_count += 1
                if any(token in error_text for token in ("winerror 10054", "connection reset", "连接")):
                    self.connection_error_event_count += 1
            if attempt < self.config.max_retries:
                self.retry_count += 1
                delay = 0.8 * (2**attempt)
                if execution:
                    delay = min(delay, execution.check())
                logger.warning(
                    "model_request_retry endpoint=%s attempt=%s/%s delay_seconds=%.1f error=%s",
                    url,
                    attempt + 1,
                    self.config.max_retries,
                    delay,
                    last_error,
                )
                time.sleep(delay)
        # 网络请求失败后对当前接口启用短时熔断。
        self._blocked_until[url] = time.monotonic() + 30
        raise RuntimeError(f"模型接口调用失败: {last_error}") from last_error

    def metrics(self) -> dict[str, int]:
        return {
            "model_request_attempt_count": self.request_attempt_count,
            "model_retry_count": self.retry_count,
            "model_timeout_event_count": self.timeout_event_count,
            "model_connection_error_event_count": self.connection_error_event_count,
            "chat_prompt_tokens": self.chat_prompt_tokens,
            "chat_completion_tokens": self.chat_completion_tokens,
            "embedding_tokens": self.embedding_tokens,
            "rerank_tokens": self.rerank_tokens,
            "model_json_retry_count": self.json_retry_count,
        }

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [float(value) / norm for value in vector]
