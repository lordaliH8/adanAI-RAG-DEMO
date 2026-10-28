import json
import sqlite3

import pytest

from app.conversations import ConversationError, ConversationStore, interruption_result
from app.observability import Observability, Redactor, TraceRecorder
from types import SimpleNamespace


def answer(**extra):
    return {"status": "answered", "answer": "پاسخ بر پایه داده", "sources": [{"source_url": "https://example.org/source"}],
            "sql_results": [{"sql": "SELECT amount_irr FROM transport_offers", "rows": [{"amount_irr": 1001}]}],
            "conversation_state": {"destination_fa": "مشهد", "adults": 2},
            "offered_options": [{"option_id": "actual-record-id", "ordinal": 1, "evidence": {"result_index": 0, "row_index": 0}}],
            "trace": [{"step": "model_call", "data": {"messages": [{"role": "system", "content": "دستور واقعی"}]}}], **extra}


def test_full_results_and_stable_ids_survive_reopening(tmp_path):
    path = tmp_path / "conversations.sqlite"
    store = ConversationStore(path)
    conversation = store.create("سفر خانوادگی")
    turn = store.begin_turn(conversation["id"], "برای دو نفر چه پیشنهادی داری؟", "turn-1")
    result = store.complete_turn(turn["turn_id"], answer())
    reopened = ConversationStore(path)
    saved = reopened.get(conversation["id"])
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][0]["id"] == result["user_message_id"]
    assert saved["messages"][1]["id"] == result["assistant_message_id"]
    assert saved["messages"][1]["result"] == result
    assert saved["messages"][1]["result"]["sql_results"] == answer()["sql_results"]
    assert reopened.list()[0]["message_count"] == 2


def test_idempotency_replays_once_and_conflicting_or_pending_keys_are_rejected(tmp_path):
    store = ConversationStore(tmp_path / "chat.sqlite")
    identity = store.create()["id"]
    turn = store.begin_turn(identity, "قیمت چقدر است؟", "same-key")
    for key, question, code in [("same-key", "قیمت چقدر است؟", "conversation_busy"),
                                ("same-key", "پرسش متفاوت", "idempotency_conflict"),
                                ("another-key", "پرسش بعدی", "conversation_busy")]:
        with pytest.raises(ConversationError) as error:
            store.begin_turn(identity, question, key)
        assert error.value.code == code
    result = store.complete_turn(turn["turn_id"], answer())
    assert store.complete_turn(turn["turn_id"], answer(answer="نباید جایگزین شود")) == result
    assert store.begin_turn(identity, "قیمت چقدر است؟", "same-key")["replay"] == result
    assert len(store.get(identity)["messages"]) == 2
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT request_key_hash FROM turns").fetchone()[0] != "same-key"


def test_context_uses_only_actual_messages_and_separate_complete_trip_state(tmp_path):
    store = ConversationStore(tmp_path / "chat.sqlite")
    identity = store.create()["id"]
    state = {"destination_fa": "مشهد", "adults": 2, "departure_date": "2026-11-17"}
    options = [{"option_id": "source-id-1", "ordinal": 1, "nights": [{"service_date": "2026-11-17", "amount_irr": 1001}]}]
    turn = store.begin_turn(identity, "دو بزرگسال به مشهد", "first")
    store.complete_turn(turn["turn_id"], answer(answer="الف" * 6000, conversation_state=state, offered_options=options))
    pending = store.begin_turn(identity, "همان گزینه اول را بررسی کن", "follow-up")
    context = store.context(identity, exclude_turn_id=pending["turn_id"])
    assert [item["role"] for item in context["history"]] == ["user", "assistant"]
    assert len(context["history"][-1]["content"]) == 4000
    assert context["conversation_state"] == state
    assert context["offered_options"] == options
    assert "همان گزینه" not in json.dumps(context["history"], ensure_ascii=False)


def test_explicit_empty_state_replaces_prior_state_but_interruptions_do_not(tmp_path):
    store = ConversationStore(tmp_path / "chat.sqlite")
    identity = store.create()["id"]
    first = store.begin_turn(identity, "مشهد برای دو نفر", "first")
    store.complete_turn(first["turn_id"], answer())
    for index in range(7):
        turn = store.begin_turn(identity, "دوباره تلاش می‌کنم", str(index))
        store.complete_turn(turn["turn_id"], interruption_result())
    assert store.context(identity)["conversation_state"] == answer()["conversation_state"]
    assert len(store.context(identity)["history"]) == 10
    changed = store.begin_turn(identity, "سفر قبلی را کنار بگذار", "changed")
    store.complete_turn(changed["turn_id"], answer(conversation_state={}, offered_options=[]))
    assert store.context(identity)["conversation_state"] == {}
    assert store.context(identity)["offered_options"] == []


def test_startup_recovers_interrupted_turn_once_and_allows_new_messages(tmp_path):
    path = tmp_path / "chat.sqlite"
    store = ConversationStore(path)
    identity = store.create()["id"]
    store.begin_turn(identity, "پرسش هنگام خاموش شدن", "old")
    restarted = ConversationStore(path)
    assert restarted.recover_interrupted() == 1
    assert restarted.recover_interrupted() == 0
    assert restarted.get(identity)["messages"][-1]["result"]["error"]["code"] == "request_interrupted"
    assert restarted.begin_turn(identity, "پرسش هنگام خاموش شدن", "old")["replay"]["status"] == "error"
    restarted.begin_turn(identity, "تلاش جدید", "new")


def test_delete_and_clear_respect_pending_turns_and_cascade_without_touching_business_db(tmp_path):
    business = tmp_path / "travel.sqlite"
    business.write_bytes(b"business database remains separate")
    store = ConversationStore(tmp_path / "conversations.sqlite")
    identity = store.create()["id"]
    turn = store.begin_turn(identity, "پرسش من", "active")
    for operation in [lambda: store.delete(identity), store.clear]:
        with pytest.raises(ConversationError, match="active"):
            operation()
    store.complete_turn(turn["turn_id"], answer())
    store.delete(identity)
    assert store.list() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    store.create()
    store.clear()
    assert store.list() == []
    assert business.read_bytes() == b"business database remains separate"
    with pytest.raises(ConversationError) as error:
        store.get(identity)
    assert error.value.status_code == 404


def test_redactor_preserves_long_farsi_prompts_and_deep_native_tool_protocol():
    prompt = "متن واقعی دستور فارسی؛ " * 1500
    native = {"request": {"messages": [{"role": "system", "content": prompt},
              {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {
                  "name": "calculate", "arguments": '{"expression":"1001/10","precision":1}'}}]},
              {"role": "tool", "tool_call_id": "call-1", "content": '{"value":"100.1","unit":"تومان"}'}],
              "response_format": {"json_schema": {"schema": {"properties": {"nested": {"items": {"properties": {
                  "actual": {"type": "string", "enum": ["actual-source-id"]}}}}}}}}}}
    assert Redactor().clean(native) == native


def test_redactor_removes_actual_secrets_and_sensitive_headers_but_keeps_token_usage():
    secret = "private-key-marker-123"
    result = Redactor([secret]).clean({"headers": {"Authorization": "Bearer hidden", "X-API-Key": "other-secret", "Cookie": "session"},
        "messages": [{"content": "دستور فارسی " + secret + " Bearer hidden-token"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20}, "refresh_token": "hidden"})
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in [secret, "hidden-token", "other-secret", '"session"']:
        assert forbidden not in encoded
    assert result["usage"] == {"prompt_tokens": 100, "completion_tokens": 20}
    assert result["messages"][0]["content"].startswith("دستور فارسی ")


def test_redactor_marks_budget_truncation_explicitly():
    result = Redactor(max_bytes=500).clean({"messages": [{"content": "سلام" * 1000}], "tail": "extra"})
    assert result["_trace_truncated"] is True
    assert "TRUNCATED" in json.dumps(result)
    assert len(json.dumps(result).encode()) < 2000


@pytest.mark.asyncio
async def test_trace_recorder_bounds_request_total_and_preserves_model_call_fields():
    recorder = TraceRecorder("test", Observability(SimpleNamespace(langfuse_enabled=False)))
    await recorder.add("model_call", "started", "دستور واقعی", data={"messages": [{"role": "system", "content": "الف" * 20000}],
        "response_format": {"type": "json_schema"}, "tools": [{"type": "function"}]})
    assert recorder.events[0]["data"]["messages"][0]["content"] == "الف" * 20000
    for index in range(20):
        await recorder.add("model_call", "completed", "پاسخ واقعی", data={"response": "ب" * 100000, "usage": {"prompt_tokens": index}})
    assert len(json.dumps(recorder.events, ensure_ascii=False).encode()) < 1_700_000
    assert any(event["data"].get("_trace_truncated") for event in recorder.events)
