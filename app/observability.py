"""Local execution evidence, with explicitly enabled optional Langfuse tracing."""
from __future__ import annotations

import inspect
import logging
import re
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from typing import Any

_REQUEST_ID = ContextVar("hamsafar_request_id", default="")
_LOG = logging.getLogger("hamsafar.execution")
_PRIVATE_KEY = re.compile(r"authorization|api.?key|secret|password|(?:access|refresh|id).?token|^(?:set-)?cookie$", re.I)


def secret_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value.get_secret_value() if hasattr(value, "get_secret_value") else value)


class Redactor:
    def __init__(self, secrets=(), *, max_bytes=2_000_000, max_depth=40, max_string_chars=250_000):
        self.secrets = tuple(str(v) for v in secrets if v and len(str(v)) >= 4)
        self.max_bytes, self.max_depth, self.max_string_chars = max_bytes, max_depth, max_string_chars

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value, flags=re.I)

    def clean(self, value: Any, depth: int = 0, *, max_bytes: int | None = None) -> Any:
        # Preserve real prompts, nested tool messages and schemas. Bound the whole
        # payload rather than silently cutting every message at 8,000 characters.
        remaining, truncated = self.max_bytes if max_bytes is None else min(self.max_bytes, max_bytes), False
        marker = "[TRUNCATED: technical trace budget exceeded]"

        def clean_text(text):
            nonlocal remaining, truncated
            text = self.text(text)
            if len(text) > self.max_string_chars:
                text, truncated = text[:self.max_string_chars], True
                text += marker
            encoded = text.encode("utf-8")
            if len(encoded) > remaining:
                text, truncated = encoded[:max(0, remaining)].decode("utf-8", errors="ignore") + marker, True
            remaining -= min(len(encoded), max(remaining, 0))
            return text

        def visit(item, level):
            nonlocal remaining, truncated
            if level > self.max_depth or remaining <= 0:
                truncated = True
                return marker
            if isinstance(item, dict):
                result = {}
                for index, (key, child) in enumerate(item.items()):
                    if index >= 2000 or remaining <= 0:
                        truncated = True
                        result["_trace_truncated"] = True
                        break
                    key = clean_text(str(key))
                    result[key] = "[REDACTED]" if _PRIVATE_KEY.search(key) else visit(child, level + 1)
                    remaining -= 4
                return result
            if isinstance(item, (list, tuple)):
                result = []
                for index, child in enumerate(item):
                    if index >= 2000 or remaining <= 0:
                        truncated = True
                        result.append(marker)
                        break
                    result.append(visit(child, level + 1))
                    remaining -= 1
                return result
            if isinstance(item, str):
                return clean_text(item)
            if item is None or isinstance(item, (int, float, bool)):
                remaining -= len(str(item)) + 1
                return item
            return clean_text(str(item))

        result = visit(value, depth)
        if truncated and isinstance(result, dict):
            result["_trace_truncated"] = True
        return result


async def deliver(emit, event: dict) -> None:
    """UI/telemetry failure must not change the answer or replay a tool."""
    if emit is None:
        return
    try:
        result = emit(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        _LOG.warning("Execution event consumer unavailable")


class Observability:
    def __init__(self, settings):
        self.redactor = Redactor([secret_value(getattr(settings, name, "")) for name in
                                  ("llm_api_key", "langfuse_secret_key")])
        self.client = None
        self.callback = None
        if not getattr(settings, "langfuse_enabled", False):
            return
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
            public = secret_value(getattr(settings, "langfuse_public_key", ""))
            private = secret_value(getattr(settings, "langfuse_secret_key", ""))
            if not public or not private:
                _LOG.warning("Langfuse enabled without complete credentials; local tracing remains active")
                return
            self.client = Langfuse(public_key=public, secret_key=private,
                                   base_url=str(getattr(settings, "langfuse_host", "https://cloud.langfuse.com")))
            self.callback = CallbackHandler(public_key=public)
        except Exception:
            self.client = self.callback = None
            _LOG.warning("Optional Langfuse initialization unavailable; local tracing remains active")

    @contextmanager
    def request_scope(self, request_id: str):
        token = _REQUEST_ID.set(request_id)
        stack = ExitStack()
        try:
            if self.client is not None:
                try:
                    span = stack.enter_context(self.client.start_as_current_observation(
                        as_type="span", name="hamsafar-question"))
                    span.update(metadata={"request_id": request_id})
                except Exception:
                    _LOG.warning("Optional remote trace unavailable")
            yield
        finally:
            try:
                stack.close()
            except Exception:
                _LOG.warning("Optional remote trace finalization unavailable")
            _REQUEST_ID.reset(token)

    def langchain_config(self, run_name="hamsafar-model") -> dict:
        config = {"run_name": run_name, "metadata": {"request_id": _REQUEST_ID.get()}}
        if self.callback is not None:
            config["callbacks"] = [self.callback]
        return config

    async def close(self):
        if self.client is not None:
            try:
                # Flush in a thread so observability never blocks the event loop.
                import asyncio
                await asyncio.to_thread(self.client.flush)
            except Exception:
                _LOG.warning("Optional remote trace flush unavailable")


class TraceRecorder:
    def __init__(self, request_id: str, observability: Observability, emit=None):
        self.request_id, self.observability, self.emit = request_id, observability, emit
        self.events: list[dict] = []
        self._trace_bytes = 0

    async def add(self, step: str, status: str, message: str, *, started=None, data=None):
        payload = {
            "step": step, "status": status, "message": message,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2) if started else 0,
            "data": data or {},
        }
        budget = 1_600_000 - self._trace_bytes
        if budget < 2000:
            payload["data"] = {"_trace_truncated": True, "reason": "Technical trace byte budget exceeded."}
        event = self.observability.redactor.clean(payload, max_bytes=max(2000, budget))
        import json
        self._trace_bytes += len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
        if len(self.events) < 100:
            self.events.append(event)
        _LOG.info("request_id=%s step=%s status=%s duration_ms=%s",
                  self.request_id, step, status, event["duration_ms"])
        await deliver(self.emit, {"type": "trace", "request_id": self.request_id, **event})
