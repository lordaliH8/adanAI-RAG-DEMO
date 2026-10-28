from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.bootstrap import initialize
from app.calendar import persian_date
from app.config import get_settings
from app.conversations import ConversationError, ConversationStore, interruption_result, now_iso
from app.llm import LLMClient, ProviderError
from app.models import ChatRequest, ConversationCreate, ConversationMessage, SearchRequest
from app.observability import Redactor, secret_value
from app.retrieval import Retriever
from app.sql import SQLService
from app.workflow import WorkflowService


@dataclass
class Services:
    settings: object
    llm: object
    retriever: object
    sql: object
    workflow: object
    startup: dict


class DemoRuntime:
    """Single-server demo admission: no derived-state rebuild during an active request."""
    def __init__(self, services):
        folder = Path(getattr(services.settings, "state_dir", services.settings.documents_dir))
        self.conversations = ConversationStore(folder / "conversations.sqlite")
        self.conversations.recover_interrupted()
        self.lock = asyncio.Lock()
        self.active = 0
        self.resetting = False
        self.jobs: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        observability = getattr(getattr(services, "workflow", None), "observability", None)
        self.redactor = getattr(observability, "redactor", None) or Redactor([
            secret_value(getattr(services.settings, name, "")) for name in ("llm_api_key", "langfuse_secret_key")])

    async def enter(self):
        async with self.lock:
            if self.resetting:
                raise HTTPException(409, {"code": "demo_resetting", "message": "Demo reset is in progress."})
            self.active += 1

    async def leave(self):
        async with self.lock:
            self.active -= 1

    @asynccontextmanager
    async def operation(self):
        await self.enter()
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await self.leave()


def _sse(event: dict) -> str:
    return "event: " + str(event.get("type", "trace")) + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _workflow_failure() -> dict:
    return {"status": "error", "answer": "پردازش با خطای کنترل‌شده متوقف شد؛ دوباره تلاش کنید.",
            "error": {"code": "workflow_unavailable", "message": "Workflow unavailable."},
            "sources": [], "sql_results": [], "calculations": [], "trace": [], "metrics": {}}


_PROVIDER_ERRORS = {
    "provider_not_configured": "Provider credentials are not configured.",
    "provider_authentication_failed": "Provider authentication failed.",
    "provider_rate_limited": "Provider rate limit reached. Try again later.",
    "provider_credit_exhausted": "Provider account credit is exhausted.",
    "provider_model_not_found": "Configured model or API endpoint was not found.",
    "provider_connection_error": "Could not connect to the provider.",
    "provider_timeout": "Provider test exceeded its time limit.",
    "provider_bad_request": "Provider rejected the test request.",
    "provider_invalid_response": "Provider returned an invalid response.",
    "invalid_model_output": "Provider returned an invalid response.",
    "provider_unavailable": "Provider is temporarily unavailable.",
}


def create_app(settings=None, services=None):
    @asynccontextmanager
    async def lifespan(app):
        llm, retriever, runtime = None, None, None
        try:
            if services is not None:
                app.state.services = services
            else:
                config = settings or get_settings()
                logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
                llm = LLMClient(config)
                retriever = Retriever(config, llm)
                startup = await initialize(config, llm, retriever)
                sql = SQLService(config.db_path)
                app.state.services = Services(config, llm, retriever, sql,
                    WorkflowService(config, llm, sql, retriever), startup)
            runtime = DemoRuntime(app.state.services)
            app.state.demo = runtime
            yield
        finally:
            if runtime is not None:
                for task in runtime.tasks:
                    task.cancel()
                await asyncio.gather(*runtime.tasks, return_exceptions=True)
            if retriever is not None:
                retriever.close()
            if llm is not None:
                await llm.close()

    api = FastAPI(title="همسفر | دستیار سفر", version="0.1.0", lifespan=lifespan,
                  description="راهنمای فارسی سفر و مقایسه هزینه بر اساس اسناد و داده‌های واقعی گردآوری‌شده؛ قیمت‌ها نیازمند بازبینی منبع هستند.")

    @api.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Pydantic errors can include user inputs; only expose field/type metadata.
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_input",
            "message": "ساختار ورودی یا طول پرسش معتبر نیست.",
            "fields": [{"field": ".".join(map(str, item["loc"])), "type": item["type"]} for item in error.errors()]}})

    @api.exception_handler(ConversationError)
    async def conversation_error(request, error):
        return JSONResponse(status_code=error.status_code,
                            content={"error": {"code": error.code, "message": error.message}})

    @api.get("/health/live")
    def live():
        return {"status": "alive"}

    @api.get("/health/ready")
    def ready(request: Request):
        service = getattr(request.app.state, "services", None)
        resetting = getattr(getattr(request.app.state, "demo", None), "resetting", False)
        if not service or resetting:
            return JSONResponse(status_code=503, content={"ready": False, "resetting": resetting})
        return {"ready": True, "as_of": service.settings.as_of,
                "records": service.startup["database"]["records"], "chunks": service.startup["documents"]["chunks"]}

    @api.get("/status")
    def status(request: Request):
        return request.app.state.services.startup

    @api.get("/demo/config")
    def demo_config(request: Request):
        config = request.app.state.services.settings
        simulation_date = str(config.as_of)
        simulation_date_fa, calendar = None, "gregorian"
        try:
            simulation_date_fa = persian_date(simulation_date)
            calendar = "jalali"
        except (ImportError, ValueError):
            pass
        try:
            observed = getattr(config, "source_observed_date", None)
        except (OSError, ValueError, TypeError):
            observed = None
        try:
            host = urlsplit(str(getattr(config, "llm_base_url", ""))).hostname or ""
        except ValueError:
            host = ""
        return {"simulation_date": simulation_date, "simulation_date_fa": simulation_date_fa,
                "simulation_calendar": calendar, "source_observed_date": observed,
                "status": "resetting" if request.app.state.demo.resetting else "ready",
                "model": str(getattr(config, "llm_model", "")), "provider_host": host,
                "provider_configured": bool(secret_value(getattr(config, "llm_api_key", ""))),
                "api_base_path": request.scope.get("root_path", "")}

    @api.post("/demo/provider-test")
    async def provider_test(request: Request):
        service, runtime = request.app.state.services, request.app.state.demo
        started = time.perf_counter()
        result = {"status": "failed", "model": str(getattr(service.settings, "llm_model", ""))}
        async with runtime.operation():
            try:
                response = await asyncio.wait_for(service.llm.complete([
                    {"role": "system", "content": "برای بررسی ارتباط، به فارسی و فقط در یک جمله کوتاه پاسخ بده."},
                    {"role": "user", "content": "لطفاً بگو ارتباط برقرار است."},
                ], max_tokens=64, temperature=0), timeout=45)
                content = response["choices"][0]["message"].get("content", "")
                if not isinstance(content, str) or not content.strip() or not re.search(r"[\u0600-\u06ff]", content):
                    raise ProviderError("provider_invalid_response", "Invalid test response.")
                result["status"] = "passed"
            except (asyncio.TimeoutError, ProviderError) as error:
                code = "provider_timeout" if isinstance(error, asyncio.TimeoutError) else error.code
                if code not in _PROVIDER_ERRORS:
                    code = "provider_invalid_response"
                result["error"] = {"code": code, "message": _PROVIDER_ERRORS[code]}
            except Exception:
                result["error"] = {"code": "provider_invalid_response", "message": _PROVIDER_ERRORS["provider_invalid_response"]}
        return {**result, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}

    async def reset_job(service, runtime, job):
        def stage(name, message, status="running"):
            job.update(status=status, stage=name)
            job["events"].append({"stage": name, "status": status, "message": message, "timestamp": now_iso()})
        try:
            stage("clearing_conversations", "Clearing persisted conversation history.")
            runtime.conversations.clear()
            stage("rebuilding_sources", "Rebuilding SQL and document indexes from preserved raw sources.")
            # Both ingestion pipelines force fresh derived state; raw/evidence/PDF
            # source files, authoring inputs, credentials and embedding cache survive.
            service.startup = await initialize(service.settings, service.llm, service.retriever, force=True)
            stage("completed", "Conversation history cleared; SQL and document indexes rebuilt.", "completed")
        except asyncio.CancelledError:
            stage("interrupted", "Reset interrupted; restart the server to initialize derived state.", "failed")
            raise
        except Exception:
            job["error"] = {"code": "reset_failed", "message": "Reset failed. Check local source files and service availability."}
            stage("failed", job["error"]["message"], "failed")
        finally:
            async with runtime.lock:
                runtime.resetting = False

    @api.post("/demo/reset", status_code=202)
    async def reset(request: Request):
        runtime, service = request.app.state.demo, request.app.state.services
        async with runtime.lock:
            if runtime.resetting or runtime.active:
                raise HTTPException(409, {"code": "demo_busy", "message": "Wait for active requests or reset to finish."})
            runtime.resetting = True
            job_id = uuid4().hex
            job = {"job_id": job_id, "status": "queued", "stage": "queued", "events": []}
            if len(runtime.jobs) >= 20:
                runtime.jobs.pop(next(iter(runtime.jobs)))
            runtime.jobs[job_id] = job
            task = asyncio.create_task(reset_job(service, runtime, job))
            runtime.tasks.add(task)
            task.add_done_callback(runtime.tasks.discard)
        return {"job_id": job_id, "status": "queued"}

    @api.get("/demo/jobs/{job_id}")
    def job_status(job_id: str, request: Request):
        job = request.app.state.demo.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, {"code": "job_not_found", "message": "Reset job not found."})
        return job

    @api.get("/conversations")
    def conversations(request: Request):
        return {"conversations": request.app.state.demo.conversations.list()}

    @api.post("/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate, request: Request):
        async with request.app.state.demo.operation():
            return request.app.state.demo.conversations.create(body.title)

    @api.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str, request: Request):
        return request.app.state.demo.conversations.get(conversation_id)

    @api.delete("/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request):
        async with request.app.state.demo.operation():
            request.app.state.demo.conversations.delete(conversation_id)
        return {"status": "deleted", "conversation_id": conversation_id}

    def streaming_response(service, runtime, question, kwargs, *, turn_id=None, replay=None):
        async def events():
            queue = asyncio.Queue()
            task = None
            trace_events, trace_request_id = [], None

            async def emit(event):
                nonlocal trace_request_id
                event = runtime.redactor.clean(event)
                if event.get("type") == "trace" and len(trace_events) < 100:
                    trace_request_id = event.get("request_id") or trace_request_id
                    trace_events.append({key: value for key, value in event.items() if key not in {"type", "request_id"}})
                await queue.put(event)

            def failure_result(interrupted=False):
                result = interruption_result() if interrupted else _workflow_failure()
                result["trace"] = trace_events
                if trace_request_id:
                    result["request_id"] = trace_request_id
                return runtime.redactor.clean(result)

            async def work():
                try:
                    answer = runtime.redactor.clean(await service.workflow.run(question, emit=emit, **kwargs))
                    if turn_id:
                        answer = runtime.conversations.complete_turn(turn_id, answer)
                    await queue.put({"type": "result", "data": answer})
                except asyncio.CancelledError:
                    if turn_id:
                        runtime.conversations.complete_turn(turn_id, failure_result(interrupted=True))
                    raise
                except Exception:
                    failure = failure_result()
                    if turn_id:
                        failure = runtime.conversations.complete_turn(turn_id, failure)
                        await queue.put({"type": "result", "data": failure})
                    else:
                        await queue.put({"type": "error", "data": failure["error"]})
            try:
                if replay is not None:
                    yield _sse({"type": "result", "data": replay, "replayed": True})
                    return
                task = asyncio.create_task(work())
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        if task.done():
                            await task
                            break
                        yield ": heartbeat\n\n"
                        continue
                    yield _sse(event)
                    if event.get("type") in {"result", "error"}:
                        break
            finally:
                # Starlette cancels its stream task group on disconnect. Shield
                # cleanup so the turn and admission count cannot remain pending.
                with anyio.CancelScope(shield=True):
                    try:
                        if task is not None:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        elif turn_id and replay is None:
                            runtime.conversations.complete_turn(turn_id, interruption_result())
                    finally:
                        await runtime.leave()
        return StreamingResponse(events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/conversations/{conversation_id}/messages/stream")
    async def conversation_message(conversation_id: str, body: ConversationMessage, request: Request):
        service, runtime = request.app.state.services, request.app.state.demo
        question = runtime.redactor.text(body.question)
        key = request.headers.get("idempotency-key")
        if key is not None and (not key or len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key)):
            raise HTTPException(422, {"code": "invalid_idempotency_key", "message": "Use a printable idempotency key of 1–128 characters."})
        await runtime.enter()
        try:
            turn = runtime.conversations.begin_turn(conversation_id, question, key)
            context = runtime.conversations.context(conversation_id, exclude_turn_id=turn["turn_id"])
            return streaming_response(service, runtime, question, context,
                                      turn_id=turn["turn_id"], replay=turn["replay"])
        except BaseException:
            await runtime.leave()
            raise

    @api.post("/chat")
    async def chat(body: ChatRequest, request: Request):
        async with request.app.state.demo.operation():
            return await request.app.state.services.workflow.run(
                body.question, history=[item.model_dump() for item in body.history], retrieval_mode=body.retrieval_mode,
                filters=body.filters.model_dump(exclude_none=True))

    @api.post("/chat/stream")
    async def stream_chat(body: ChatRequest, request: Request):
        runtime = request.app.state.demo
        await runtime.enter()
        return streaming_response(request.app.state.services, runtime, body.question,
            {"history": [item.model_dump() for item in body.history], "retrieval_mode": body.retrieval_mode,
             "filters": body.filters.model_dump(exclude_none=True)})

    @api.post("/retrieval/search")
    async def search(body: SearchRequest, request: Request):
        async with request.app.state.demo.operation():
            try:
                return await request.app.state.services.retriever.search(body.query, body.mode,
                    filters=body.filters.model_dump(exclude_none=True), limit=body.limit)
            except ProviderError as error:
                return JSONResponse(status_code=503, content={"error": {"code": error.code, "message": error.message}})
            except Exception:
                return JSONResponse(status_code=503, content={"error": {"code": "retrieval_unavailable", "message": "بازیابی در حال حاضر قابل انجام نیست."}})

    @api.post("/retrieval/compare")
    async def compare(body: SearchRequest, request: Request, include_rerank: bool = Query(False)):
        async with request.app.state.demo.operation():
            try:
                return await request.app.state.services.retriever.compare(body.query,
                    filters=body.filters.model_dump(exclude_none=True), limit=body.limit, include_rerank=include_rerank)
            except ProviderError as error:
                return JSONResponse(status_code=503, content={"error": {"code": error.code, "message": error.message}})
            except Exception:
                return JSONResponse(status_code=503, content={"error": {"code": "retrieval_unavailable", "message": "مقایسه بازیابی در حال حاضر قابل انجام نیست."}})

    @api.get("/documents/{filename}")
    def document(filename: str, request: Request):
        folder = request.app.state.services.settings.documents_dir
        if Path(filename).name != filename or not filename.endswith(".pdf") or not (folder / filename).is_file():
            raise HTTPException(404, "سند پیدا نشد.")
        return FileResponse(folder / filename, media_type="application/pdf", filename=filename,
                            content_disposition_type="inline")

    return api


app = create_app()
