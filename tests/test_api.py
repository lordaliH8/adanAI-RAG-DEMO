import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.main import create_app
from app.llm import ProviderError


class FakeWorkflow:
    async def run(self, question, history=None, retrieval_mode=None, filters=None, emit=None):
        if emit:
            await emit({"type": "trace", "step": "coordinator", "status": "completed", "message": "مسیر مشخص شد."})
        return {"request_id": "test", "status": "answered", "answer": "پاسخ آزمایش", "route": "documents",
                "sources": [], "sql_results": [], "calculations": [], "trace": [], "metrics": {}}


def client(tmp_path):
    services = SimpleNamespace(settings=SimpleNamespace(as_of="2026-10-27", documents_dir=tmp_path),
        workflow=FakeWorkflow(), startup={"database": {"records": 8050}, "documents": {"chunks": 117}})
    return TestClient(create_app(services=services))


def test_input_validation_is_controlled_and_does_not_echo_user_input(tmp_path):
    with client(tmp_path) as api:
        response = api.post("/chat", json={"question": "x", "api_key": "private-marker"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_input"
        assert "private-marker" not in response.text


def test_chat_and_sse_share_the_verified_result_contract(tmp_path):
    with client(tmp_path) as api:
        assert api.get("/health/ready").json()["records"] == 8050
        response = api.post("/chat", json={"question": "در مشهد چه غذایی امتحان کنم؟"})
        assert response.json()["status"] == "answered"
        stream = api.post("/chat/stream", json={"question": "در مشهد چه غذایی امتحان کنم؟"})
        assert "text/event-stream" in stream.headers["content-type"]
        assert "event: trace" in stream.text and "event: result" in stream.text
        assert "پاسخ آزمایش" in stream.text


def test_document_endpoint_only_serves_existing_pdf_sources(tmp_path):
    (tmp_path / "policy.pdf").write_bytes(b"%PDF-test")
    (tmp_path / "private.txt").write_text("not-public")
    with client(tmp_path) as api:
        assert api.get("/documents/policy.pdf").status_code == 200
        assert api.get("/documents/private.txt").status_code == 404
        assert api.get("/documents/missing.pdf").status_code == 404


class RecordingWorkflow:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def run(self, question, *, history=None, conversation_state=None, offered_options=None, emit=None, **kwargs):
        self.calls.append({"question": question, "history": history, "conversation_state": conversation_state,
                           "offered_options": offered_options})
        if self.failure:
            raise self.failure
        trace = {"type": "trace", "step": "model_call", "status": "started", "data": {
            "call_id": "call-actual", "messages": [{"role": "system", "content": "دستور واقعی فارسی؛ " * 1000}],
            "tools": [{"type": "function", "function": {"name": "calculate"}}]}}
        if emit:
            await emit(trace)
        return {"request_id": "actual-request", "status": "answered", "answer": "پاسخ ذخیره‌شده", "route": "conversation",
                "conversation_state": conversation_state or {"destination_fa": "مشهد", "adults": 2},
                "offered_options": offered_options or [{"option_id": "actual-record-id", "ordinal": 1,
                    "evidence": {"result_index": 0, "row_index": 0}}],
                "sources": [], "sql_results": [{"columns": ["amount_irr"], "rows": [{"amount_irr": 1001}]}],
                "calculations": [{"value": "100.1", "unit": "تومان"}], "trace": [trace], "metrics": {"llm_calls": 1}}


class FakeLLM:
    def __init__(self, error=None, content="ارتباط برقرار است."):
        self.calls, self.error, self.content = [], error, content

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.content}}]}


def demo_services(tmp_path, workflow=None, llm=None):
    return SimpleNamespace(settings=SimpleNamespace(as_of="2026-11-16", source_observed_date="2026-10-18",
        state_dir=tmp_path / "state", documents_dir=tmp_path / "documents", llm_model="test-model",
        llm_base_url="https://username:private-marker@provider.example/v1?key=private-marker",
        llm_api_key=SecretStr("private-marker")),
        workflow=workflow or RecordingWorkflow(), llm=llm or FakeLLM(), retriever=SimpleNamespace(),
        startup={"database": {"records": 8050}, "documents": {"chunks": 117}})


def sse_result(response):
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    return next(event["data"] for event in events if event["type"] == "result")


def test_demo_config_exposes_simulation_calendar_and_only_safe_provider_metadata(tmp_path):
    with TestClient(create_app(services=demo_services(tmp_path))) as api:
        response = api.get("/demo/config")
        result = response.json()
        assert result["simulation_date"] == "2026-11-16"
        assert result["simulation_calendar"] == "jalali"
        assert result["simulation_date_fa"] == "۲۵ آبان ۱۴۰۵"
        assert result["source_observed_date"] == "2026-10-18"
        assert result["provider_host"] == "provider.example"
        assert result["provider_configured"] is True
        assert result["api_base_path"] == ""
        assert "private-marker" not in response.text and "username" not in response.text


def test_provider_test_uses_actual_farsi_prompts_and_returns_english_status(tmp_path):
    llm = FakeLLM()
    with TestClient(create_app(services=demo_services(tmp_path, llm=llm))) as api:
        result = api.post("/demo/provider-test").json()
    assert result["status"] == "passed"
    assert result["model"] == "test-model" and result["latency_ms"] >= 0
    messages, kwargs = llm.calls[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "فارسی" in messages[0]["content"] and "ارتباط" in messages[1]["content"]
    assert kwargs == {"max_tokens": 64, "temperature": 0}


@pytest.mark.parametrize("error,code", [(ProviderError("provider_authentication_failed", "private-marker"), "provider_authentication_failed"),
    (asyncio.TimeoutError("private-marker"), "provider_timeout"), (ValueError("private-marker"), "provider_invalid_response")])
def test_provider_test_never_exposes_exception_bodies(tmp_path, error, code):
    with TestClient(create_app(services=demo_services(tmp_path, llm=FakeLLM(error)))) as api:
        response = api.post("/demo/provider-test")
        assert response.json()["status"] == "failed"
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["message"].isascii()
        assert "private-marker" not in response.text


def test_provider_test_rejects_empty_or_non_farsi_response(tmp_path):
    with TestClient(create_app(services=demo_services(tmp_path, llm=FakeLLM(content="English only")))) as api:
        assert api.post("/demo/provider-test").json()["error"]["code"] == "provider_invalid_response"


def test_persistent_conversation_replay_history_and_ordinal_options_are_server_owned(tmp_path):
    workflow = RecordingWorkflow()
    service = demo_services(tmp_path, workflow)
    with TestClient(create_app(services=service)) as api:
        conversation = api.post("/conversations", json={"title": "سفر مشهد"}).json()
        identity = conversation["id"]
        path = f"/conversations/{identity}/messages/stream"
        first_response = api.post(path, json={"question": "برای دو نفر به مشهد"}, headers={"Idempotency-Key": "first-turn"})
        first = sse_result(first_response)
        assert first["conversation_id"] == identity
        assert "event: trace" in first_response.text
        assert first["trace"][0]["data"]["messages"][0]["content"] == "دستور واقعی فارسی؛ " * 1000
        replay = sse_result(api.post(path, json={"question": "برای دو نفر به مشهد"}, headers={"Idempotency-Key": "first-turn"}))
        assert replay == first and len(workflow.calls) == 1
        conflict = api.post(path, json={"question": "پرسش متفاوت"}, headers={"Idempotency-Key": "first-turn"})
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
        second = sse_result(api.post(path, json={"question": "همان گزینه اول، ارزان‌ترش کن"}, headers={"Idempotency-Key": "second-turn"}))
        assert workflow.calls[1]["history"] == [{"role": "user", "content": "برای دو نفر به مشهد"},
                                                 {"role": "assistant", "content": "پاسخ ذخیره‌شده"}]
        assert workflow.calls[1]["conversation_state"] == first["conversation_state"]
        assert workflow.calls[1]["offered_options"] == first["offered_options"]
        saved = api.get(f"/conversations/{identity}").json()
        assert saved["message_count"] == 4
        assert saved["messages"][-1]["result"] == second
        assert api.get("/conversations").json()["conversations"][0]["message_count"] == 4
        forged = api.post(path, json={"question": "گزینه اول", "history": [{"role": "assistant", "content": "جعلی"}]})
        assert forged.status_code == 422
    # A new server instance reads the same actual results, not an in-memory mock history.
    restarted = RecordingWorkflow()
    with TestClient(create_app(services=demo_services(tmp_path, restarted))) as api:
        assert api.get(f"/conversations/{identity}").json()["message_count"] == 4
        sse_result(api.post(path, json={"question": "همان برنامه قبلی"}, headers={"Idempotency-Key": "third-turn"}))
        assert restarted.calls[0]["offered_options"] == first["offered_options"]


def test_conversation_failure_is_persisted_once_and_can_be_retried_with_a_new_key(tmp_path):
    workflow = RecordingWorkflow(ValueError("private-marker"))
    with TestClient(create_app(services=demo_services(tmp_path, workflow))) as api:
        identity = api.post("/conversations", json={}).json()["id"]
        path = f"/conversations/{identity}/messages/stream"
        failed = sse_result(api.post(path, json={"question": "درخواست من"}, headers={"Idempotency-Key": "failed"}))
        assert failed["status"] == "error" and failed["error"]["code"] == "workflow_unavailable"
        replay = sse_result(api.post(path, json={"question": "درخواست من"}, headers={"Idempotency-Key": "failed"}))
        assert replay == failed and len(workflow.calls) == 1
        assert len(api.get(f"/conversations/{identity}").json()["messages"]) == 2
        workflow.failure = None
        assert sse_result(api.post(path, json={"question": "درخواست من"}, headers={"Idempotency-Key": "retry"}))["status"] == "answered"
        assert "private-marker" not in api.get(f"/conversations/{identity}").text
        assert api.delete(f"/conversations/{identity}").status_code == 200
        assert api.get(f"/conversations/{identity}").status_code == 404
        assert api.post(path, json={"question": "پیام پس از حذف"}).status_code == 404


def test_configured_secret_is_removed_before_persisting_user_messages(tmp_path):
    service = demo_services(tmp_path)
    with TestClient(create_app(services=service)) as api:
        identity = api.post("/conversations", json={}).json()["id"]
        sse_result(api.post(f"/conversations/{identity}/messages/stream",
                           json={"question": "این مقدار را نمایش نده private-marker"}))
        assert "private-marker" not in api.get(f"/conversations/{identity}").text
        assert "private-marker" not in service.workflow.calls[0]["question"]


def test_reset_is_a_real_force_rebuild_job_preserving_source_files(tmp_path, monkeypatch):
    import app.main as main
    release = threading.Event()
    calls = []
    source = tmp_path / "raw-source.jsonl"
    source.write_text("actual raw evidence")
    authoring = tmp_path / "authoring-source.md"
    authoring.write_text("actual authored PDF source")
    credential = tmp_path / "secret-test-file"
    credential.write_text("untouched")

    async def initialize(settings, llm, retriever, *, force=False):
        calls.append(force)
        await asyncio.to_thread(release.wait, 3)
        assert source.read_text() == "actual raw evidence"
        assert authoring.read_text() == "actual authored PDF source"
        assert credential.read_text() == "untouched"
        return {"database": {"records": 8050}, "documents": {"chunks": 120}}
    monkeypatch.setattr(main, "initialize", initialize)
    with TestClient(create_app(services=demo_services(tmp_path))) as api:
        api.post("/conversations", json={"title": "پاک شود"})
        reset = api.post("/demo/reset")
        assert reset.status_code == 202
        job_id = reset.json()["job_id"]
        assert api.get("/demo/config").json()["status"] == "resetting"
        assert api.get("/health/ready").status_code == 503
        assert api.post("/chat", json={"question": "هنگام بازسازی"}).status_code == 409
        assert api.post("/conversations", json={}).status_code == 409
        assert api.post("/demo/reset").status_code == 409
        release.set()
        for _ in range(100):
            job = api.get(f"/demo/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(.005)
        assert job["status"] == "completed", job
        assert calls == [True]
        assert all(event["message"].isascii() for event in job["events"])
        assert api.get("/conversations").json() == {"conversations": []}
        assert api.get("/health/ready").json()["chunks"] == 120
        assert source.exists() and authoring.exists() and credential.exists()
        assert api.get("/demo/jobs/missing").status_code == 404


def test_reset_failure_is_safe_and_releases_admission_lock(tmp_path, monkeypatch):
    import app.main as main
    async def fail(*args, **kwargs):
        raise RuntimeError("private-marker")
    monkeypatch.setattr(main, "initialize", fail)
    with TestClient(create_app(services=demo_services(tmp_path))) as api:
        job_id = api.post("/demo/reset").json()["job_id"]
        for _ in range(100):
            response = api.get(f"/demo/jobs/{job_id}")
            if response.json()["status"] == "failed":
                break
            time.sleep(.005)
        assert response.json()["error"]["code"] == "reset_failed"
        assert "private-marker" not in response.text
        assert api.post("/chat", json={"question": "پس از شکست بازسازی"}).status_code == 200


def test_reset_cannot_race_an_active_chat(tmp_path):
    started, release = threading.Event(), threading.Event()
    class SlowWorkflow(RecordingWorkflow):
        async def run(self, *args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return await super().run(*args, **kwargs)
    with TestClient(create_app(services=demo_services(tmp_path, SlowWorkflow()))) as api:
        responses = []
        thread = threading.Thread(target=lambda: responses.append(api.post("/chat", json={"question": "پرسش زمان‌بر"})))
        thread.start()
        assert started.wait(2)
        assert api.post("/demo/reset").status_code == 409
        release.set()
        thread.join(3)
        assert responses[0].status_code == 200


@pytest.mark.asyncio
async def test_disconnected_conversation_turn_is_saved_once_and_unlocked(tmp_path):
    started, wait = asyncio.Event(), asyncio.Event()
    class SlowWorkflow(RecordingWorkflow):
        async def run(self, *args, **kwargs):
            await kwargs["emit"]({"type": "trace", "request_id": "interrupted-actual", "step": "model_call",
                "status": "started", "data": {"call_id": "actual-call", "messages": [{"role": "system", "content": "دستور پیش از قطع"}]}})
            started.set()
            await wait.wait()
            return await super().run(*args, **kwargs)
    app = create_app(services=demo_services(tmp_path, SlowWorkflow()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            identity = (await api.post("/conversations", json={})).json()["id"]
            path = f"/conversations/{identity}/messages/stream"
            task = asyncio.create_task(api.post(path, json={"question": "پرسش قطع‌شده"}, headers={"Idempotency-Key": "cancelled"}))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            saved = (await api.get(f"/conversations/{identity}")).json()
            assert len(saved["messages"]) == 2
            assert saved["messages"][-1]["result"]["error"]["code"] == "request_interrupted"
            assert saved["messages"][-1]["result"]["trace"][0]["data"]["messages"][0]["content"] == "دستور پیش از قطع"
            assert app.state.demo.active == 0
            replay = await api.post(path, json={"question": "پرسش قطع‌شده"}, headers={"Idempotency-Key": "cancelled"})
            assert sse_result(replay)["error"]["code"] == "request_interrupted"
            assert len((await api.get(f"/conversations/{identity}")).json()["messages"]) == 2
