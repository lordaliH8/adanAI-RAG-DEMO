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
_PRIVATE_KEY = re.compile(r"authorization|api.?key|secret|password|access.?token", re.I)


def secret_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value.get_secret_value() if hasattr(value, "get_secret_value") else value)


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = tuple(str(v) for v in secrets if v and len(str(v)) >= 4)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value, flags=re.I)

    def clean(self, value: Any, depth: int = 0) -> Any:
        if depth > 7:
            return "[bounded]"
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if _PRIVATE_KEY.search(str(k)) else self.clean(v, depth + 1)
                    for k, v in list(value.items())[:50]}
        if isinstance(value, (list, tuple)):
            return [self.clean(v, depth + 1) for v in value[:100]]
        if isinstance(value, str):
            return self.text(value)[:8000]
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.text(str(value))[:200]


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

    async def add(self, step: str, status: str, message: str, *, started=None, data=None):
        event = self.observability.redactor.clean({
            "step": step, "status": status, "message": message,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2) if started else 0,
            "data": data or {},
        })
        if len(self.events) < 100:
            self.events.append(event)
        _LOG.info("request_id=%s step=%s status=%s duration_ms=%s",
                  self.request_id, step, status, event["duration_ms"])
        await deliver(self.emit, {"type": "trace", "request_id": self.request_id, **event})
