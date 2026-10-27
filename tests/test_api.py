from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app


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
