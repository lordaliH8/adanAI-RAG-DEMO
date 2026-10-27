from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.bootstrap import initialize
from app.config import get_settings
from app.llm import LLMClient, ProviderError
from app.models import ChatRequest, SearchRequest
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


def create_app(settings=None, services=None):
    @asynccontextmanager
    async def lifespan(app):
        if services is not None:
            app.state.services = services
            yield
            return
        config = settings or get_settings()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        llm, retriever = LLMClient(config), None
        try:
            retriever = Retriever(config, llm)
            startup = await initialize(config, llm, retriever)
            sql = SQLService(config.db_path)
            app.state.services = Services(config, llm, retriever, sql, WorkflowService(config, llm, sql, retriever), startup)
            yield
        finally:
            if retriever is not None:
                retriever.close()
            await llm.close()

    api = FastAPI(title="همسفر | دستیار سفر", version="0.1.0", lifespan=lifespan,
                  description="راهنمای فارسی سفر و مقایسه هزینه بر اساس اسناد و داده‌های واقعی گردآوری‌شده؛ قیمت‌ها نیازمند بازبینی منبع هستند.")

    @api.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Pydantic errors can include user inputs; only expose field/type metadata.
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_input",
            "message": "ساختار ورودی یا طول پرسش معتبر نیست.",
            "fields": [{"field": ".".join(map(str, item["loc"])), "type": item["type"]} for item in error.errors()]}})

    @api.get("/health/live")
    def live():
        return {"status": "alive"}

    @api.get("/health/ready")
    def ready(request: Request):
        service = getattr(request.app.state, "services", None)
        if not service:
            return JSONResponse(status_code=503, content={"ready": False})
        return {"ready": True, "as_of": service.settings.as_of,
                "records": service.startup["database"]["records"], "chunks": service.startup["documents"]["chunks"]}

    @api.get("/status")
    def status(request: Request):
        return request.app.state.services.startup

    @api.post("/chat")
    async def chat(body: ChatRequest, request: Request):
        return await request.app.state.services.workflow.run(
            body.question, history=[item.model_dump() for item in body.history], retrieval_mode=body.retrieval_mode,
            filters=body.filters.model_dump(exclude_none=True))

    @api.post("/chat/stream")
    async def stream_chat(body: ChatRequest, request: Request):
        service = request.app.state.services

        async def events():
            queue = asyncio.Queue()
            async def emit(event):
                await queue.put(event)
            async def work():
                try:
                    answer = await service.workflow.run(body.question, history=[item.model_dump() for item in body.history],
                        retrieval_mode=body.retrieval_mode, filters=body.filters.model_dump(exclude_none=True), emit=emit)
                    await queue.put({"type": "result", "data": answer})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await queue.put({"type": "error", "data": {"code": "workflow_unavailable",
                        "message": "پردازش با خطای کنترل‌شده متوقف شد؛ دوباره تلاش کنید."}})
            task = asyncio.create_task(work())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except asyncio.TimeoutError:
                        if task.done():
                            await task
                            break
                        yield ": heartbeat\n\n"
                        continue
                    yield "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                    if event["type"] in {"result", "error"}:
                        break
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.post("/retrieval/search")
    async def search(body: SearchRequest, request: Request):
        try:
            return await request.app.state.services.retriever.search(body.query, body.mode,
                filters=body.filters.model_dump(exclude_none=True), limit=body.limit)
        except ProviderError as error:
            return JSONResponse(status_code=503, content={"error": {"code": error.code, "message": error.message}})
        except Exception:
            return JSONResponse(status_code=503, content={"error": {"code": "retrieval_unavailable", "message": "بازیابی در حال حاضر قابل انجام نیست."}})

    @api.post("/retrieval/compare")
    async def compare(body: SearchRequest, request: Request, include_rerank: bool = Query(False)):
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
