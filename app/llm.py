"""Paced, bounded Chat Completions adapter; no credential or raw-body logging."""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from langchain_core.messages import convert_to_messages, convert_to_openai_messages
from langchain_openai import ChatOpenAI

from app.observability import Observability, deliver, secret_value


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code, self.message, self.retryable = code, message, retryable
        super().__init__(message)


def classify_error(error: Exception) -> ProviderError:
    if isinstance(error, ProviderError):
        return error
    # OpenAI-compatible providers sometimes encode exhausted account credit as
    # HTTP429. Read only structured classification fields; never expose/log the
    # provider message, balance, request body or credentials.
    indicators = [getattr(error, "code", None), getattr(error, "type", None)]
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        response = getattr(error, "response", None)
        if response is not None:
            try:
                body = response.json()
            except (ValueError, TypeError, AttributeError, httpx.ResponseNotRead):
                body = None
    if isinstance(body, dict):
        for payload in (body, body.get("error")):
            if isinstance(payload, dict):
                indicators.extend((payload.get("code"), payload.get("type")))
    if any(isinstance(value, str) and value.strip().lower() == "insufficient_quota" for value in indicators):
        return ProviderError("provider_credit_exhausted", "اعتبار حساب ارائه‌دهنده مدل کافی نیست؛ موجودی حساب و تنظیمات اعتبار یا API را بررسی و در صورت نیاز شارژ کنید.", False)
    status = getattr(error, "status_code", None)
    if status is None and getattr(error, "response", None) is not None:
        status = getattr(error.response, "status_code", None)
    if status in {401, 403}:
        return ProviderError("provider_authentication_failed", "ارائه‌دهنده مدل دسترسی را نپذیرفت؛ تنظیمات دسترسی باید بررسی شود.")
    if status == 429:
        return ProviderError("provider_rate_limited", "محدودیت سرعت ارائه‌دهنده فعلاً اجازه پاسخ نمی‌دهد. کمی بعد دوباره تلاش کنید.", True)
    if status == 404:
        return ProviderError("provider_model_not_found", "مدل یا مسیر تنظیم‌شده در ارائه‌دهنده پیدا نشد.")
    if isinstance(status, int) and status >= 500:
        return ProviderError("provider_unavailable", "ارائه‌دهنده مدل موقتاً در دسترس نیست.", True)
    if status is not None:
        return ProviderError("provider_bad_request", "ارائه‌دهنده قالب درخواست را نپذیرفت؛ تنظیم مدل و API باید بررسی شود.")
    if isinstance(error, (httpx.TimeoutException, asyncio.TimeoutError)) or "Timeout" in type(error).__name__:
        return ProviderError("provider_timeout", "مهلت پاسخ ارائه‌دهنده مدل پایان یافت؛ دوباره تلاش کنید.", True)
    if isinstance(error, httpx.TransportError) or "Connection" in type(error).__name__:
        return ProviderError("provider_connection_error", "ارتباط با ارائه‌دهنده مدل برقرار نشد.", True)
    return ProviderError("provider_invalid_response", "پاسخ ارائه‌دهنده در قالب مورد انتظار قابل استفاده نبود.")


def retry_delay(error: Exception, attempt: int) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value:
        try:
            return max(0.0, min(float(value), 30.0))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(str(value))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, min((parsed - datetime.now(timezone.utc)).total_seconds(), 30.0))
            except (TypeError, ValueError, OverflowError):
                pass
    status = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    # A subsecond retry rarely clears a rolling request-rate window.
    if status == 429:
        return min(15.0 * (2 ** attempt), 30.0)
    return min(0.5 * (2 ** attempt), 8.0)


class LLMClient:
    def __init__(self, settings):
        self.settings = settings
        self._key = secret_value(settings.llm_api_key)
        self._base_url = str(settings.llm_base_url).rstrip("/")
        self._retries = max(0, min(int(settings.llm_max_retries), 3))
        self._interval = max(0.0, float(settings.llm_min_interval_seconds))
        self._gate = asyncio.Semaphore(1)
        self._last_start = 0.0
        self.observability = Observability(settings)
        self._http = httpx.AsyncClient(timeout=float(settings.llm_timeout_seconds),
                                       limits=httpx.Limits(max_connections=4, max_keepalive_connections=2))
        self._chat = ChatOpenAI(
            model=str(settings.llm_model), api_key=self._key or "not-configured",
            base_url=self._base_url, timeout=float(settings.llm_timeout_seconds),
            max_retries=0, use_responses_api=False, http_async_client=self._http,
        )
        self._usage = {"chat_calls": 0, "embedding_calls": 0, "retries": 0,
                       "prompt_tokens": 0, "completion_tokens": 0, "embedding_tokens": 0,
                       "responses_with_usage": 0}

    async def _request(self, operation, *, kind: str, emit=None):
        if not self._key:
            raise ProviderError("provider_not_configured", "کلید دسترسی مدل تنظیم نشده است.")
        for attempt in range(self._retries + 1):
            try:
                async with self._gate:
                    pause = self._interval - (time.monotonic() - self._last_start)
                    if pause > 0:
                        await asyncio.sleep(pause)
                    self._last_start = time.monotonic()
                    return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                safe = classify_error(error)
                if not safe.retryable or attempt == self._retries:
                    raise safe from None
                delay = retry_delay(error, attempt)
                self._usage["retries"] += 1
                await deliver(emit, {"type": "provider_retry", "operation": kind,
                                     "attempt": attempt + 1, "delay_seconds": delay,
                                     "code": safe.code, "message": safe.message})
                await asyncio.sleep(delay)
        raise ProviderError("provider_unavailable", "ارائه‌دهنده مدل در دسترس نیست.")

    async def complete(self, messages: list[dict], *, response_format=None, tools=None,
                       tool_choice=None, max_tokens=None, temperature=0, emit=None) -> dict:
        kwargs = {"temperature": temperature}
        for key, value in (("response_format", response_format), ("tools", tools),
                           ("tool_choice", tool_choice), ("max_tokens", max_tokens)):
            if value is not None:
                kwargs[key] = value
        # Malformed caller messages fail before any network request.
        try:
            converted = convert_to_messages(messages)
        except Exception:
            raise ProviderError("invalid_message_protocol", "ساختار پیام‌های گفتگو یا ابزار معتبر نیست.") from None

        async def invoke():
            return await self._chat.ainvoke(converted, config=self.observability.langchain_config(), **kwargs)

        result = await self._request(invoke, kind="chat", emit=emit)
        self._usage["chat_calls"] += 1
        try:
            if getattr(result, "invalid_tool_calls", None):
                raise ValueError("Invalid native tool arguments")
            message = convert_to_openai_messages(result)
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise ValueError("Invalid assistant response")
            if isinstance(message.get("content"), list):
                message["content"] = "".join(block.get("text", "") for block in message["content"]
                                               if isinstance(block, dict) and block.get("type") == "text")
            usage = getattr(result, "usage_metadata", None) or {}
            metadata = getattr(result, "response_metadata", None) or {}
            raw_usage = metadata.get("token_usage") or {}
            normalized = {
                "prompt_tokens": usage.get("input_tokens", raw_usage.get("prompt_tokens")),
                "completion_tokens": usage.get("output_tokens", raw_usage.get("completion_tokens")),
                "total_tokens": usage.get("total_tokens", raw_usage.get("total_tokens")),
            }
            if any(isinstance(value, int) for value in normalized.values()):
                self._usage["responses_with_usage"] += 1
            for key in ("prompt_tokens", "completion_tokens"):
                if isinstance(normalized[key], int):
                    self._usage[key] += normalized[key]
            return {"id": str(getattr(result, "id", "") or ""),
                    "model": metadata.get("model_name", str(self.settings.llm_model)),
                    "choices": [{"index": 0, "message": message,
                                 "finish_reason": metadata.get("finish_reason", "stop")}],
                    "usage": normalized}
        except Exception:
            raise ProviderError("invalid_model_output", "پاسخ مدل یا آرگومان ابزار در قالب معتبر دریافت نشد.") from None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ProviderError("invalid_embedding_input", "ورودی بردارسازی باید متن غیرخالی باشد.")
        vectors = []
        batch_size = max(1, min(int(self.settings.embedding_batch_size), 128))
        dimension = int(getattr(self.settings, "embedding_dimensions", 1536))
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]

            async def request_batch():
                response = await self._http.post(
                    f"{self._base_url}/embeddings", headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": str(self.settings.embedding_model), "input": batch})
                response.raise_for_status()
                return response.json()

            payload = await self._request(request_batch, kind="embedding")
            self._usage["embedding_calls"] += 1
            try:
                data = sorted(payload["data"], key=lambda row: row["index"])
                if [row["index"] for row in data] != list(range(len(batch))):
                    raise ValueError("Embedding indices mismatch")
                for row in data:
                    vector = row["embedding"]
                    if not isinstance(vector, list) or len(vector) != dimension:
                        raise ValueError("Embedding dimension mismatch")
                    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
                        raise ValueError("Embedding is not finite numeric data")
                    vectors.append([float(x) for x in vector])
                tokens = (payload.get("usage") or {}).get("total_tokens")
                if isinstance(tokens, int):
                    self._usage["embedding_tokens"] += tokens
            except (KeyError, TypeError, ValueError):
                raise ProviderError("invalid_embedding_response", "پاسخ بردارسازی با تعداد، ابعاد یا نوع عددی مورد انتظار سازگار نیست.") from None
        return vectors

    def usage_snapshot(self) -> dict:
        return dict(self._usage)

    async def close(self):
        await self._http.aclose()
        await self.observability.close()
