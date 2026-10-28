"""Real installed ChatOpenAI/OpenAI SDK requests against an HTTP-only mock.

No SDK method stub and no live credentials: verify the actual serialized wire
contract for both configured providers and fail before unconfigured requests.
"""
import json

import httpx
import pytest

from app.config import Settings
from app.llm import LLMClient, ProviderError


@pytest.fixture(autouse=True)
def isolate_provider_environment(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def completion(message, finish_reason="stop"):
    return {"id": "chatcmpl-wire-test", "object": "chat.completion", "created": 1,
            "model": "gpt-4.1", "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}


@pytest.mark.parametrize("provider,host", [("openai", "api.openai.com"), ("avalai", "api.avalai.org")])
@pytest.mark.asyncio
async def test_actual_sdk_http_strict_json_native_tool_roundtrip_and_embeddings(provider, host, monkeypatch):
    # Ambient SDK variables must not redirect or replace the selected profile.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://never-selected.invalid/v1")
    key = f"mock-{provider}-credential"
    config = Settings(_env_file=None, llm_provider=provider, llm_api_key=key,
                      llm_min_interval_seconds=0, llm_max_retries=0, embedding_dimensions=3)
    requests = []
    native = {"id": "call-native-preserved", "type": "function", "function": {
        "name": "calculate", "arguments": '{"expression":"2+3","precision":0,"unit":"تومان"}'}}
    chat_count = 0

    def handle(request):
        nonlocal chat_count
        body = json.loads(request.content)
        requests.append((request, body))
        assert request.url.host == host
        assert request.headers["authorization"] == f"Bearer {key}"
        if request.url.path == "/v1/embeddings":
            assert body == {"model": "text-embedding-3-small", "input": ["سلام"], "encoding_format": "float", "dimensions": 3}
            return httpx.Response(200, json={"object": "list", "model": body["model"], "data": [
                {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}], "usage": {"total_tokens": 1}})
        assert request.url.path == "/v1/chat/completions"
        chat_count += 1
        if chat_count == 1:
            assert body["response_format"]["type"] == "json_schema"
            assert body["response_format"]["json_schema"]["strict"] is True
            assert body["messages"][0]["content"] == "فقط به فارسی و مطابق ساختار خواسته‌شده پاسخ بده."
            return httpx.Response(200, json=completion({"role": "assistant", "content": '{"ok":true}'}))
        if chat_count == 2:
            assert body["tools"][0]["function"]["strict"] is True
            assert body["tools"][0]["function"]["description"] == "محاسبه عددی را با ابزار انجام بده."
            assert body["tool_choice"] == "auto"
            return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [native]}, "tool_calls"))
        assert body["messages"][-1] == {"role": "tool", "tool_call_id": "call-native-preserved", "content": '{"value":"5"}'}
        assert body["messages"][-2]["tool_calls"][0]["id"] == "call-native-preserved"
        return httpx.Response(200, json=completion({"role": "assistant", "content": "پنج تومان"}))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = LLMClient(config, http_client=http)
    try:
        first = await client.complete([
            {"role": "system", "content": "فقط به فارسی و مطابق ساختار خواسته‌شده پاسخ بده."},
            {"role": "user", "content": "سلام"}], response_format={"type": "json_schema", "json_schema": {
                "name": "wire_test", "strict": True, "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}})
        assert first["choices"][0]["message"]["content"] == '{"ok":true}'
        tool_response = await client.complete([{"role": "user", "content": "دو و سه را جمع کن."}], tools=[{
            "type": "function", "function": {"name": "calculate", "description": "محاسبه عددی را با ابزار انجام بده.", "strict": True,
                "parameters": {"type": "object", "properties": {"expression": {"type": "string"}, "precision": {"type": "integer"}, "unit": {"type": "string"}}, "required": ["expression", "precision", "unit"], "additionalProperties": False}}}], tool_choice="auto")
        native_message = tool_response["choices"][0]["message"]
        assert native_message["tool_calls"][0]["id"] == "call-native-preserved"
        final = await client.complete([{"role": "user", "content": "دو و سه را جمع کن."}, native_message,
            {"role": "tool", "tool_call_id": "call-native-preserved", "content": '{"value":"5"}'}])
        assert final["choices"][0]["message"]["content"] == "پنج تومان"
        assert await client.embed(["سلام"]) == [[0.1, 0.2, 0.3]]
        assert len(requests) == 4
        assert client.usage_snapshot()["chat_calls"] == 3
        assert client.usage_snapshot()["embedding_calls"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_official_key_alias_reaches_only_official_host(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-official-only")
    seen = []
    def handle(request):
        seen.append(request)
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == "Bearer mock-official-only"
        return httpx.Response(200, json=completion({"role": "assistant", "content": "سلام"}))
    client = LLMClient(Settings(_env_file=None, llm_provider="openai", llm_min_interval_seconds=0),
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        await client.complete([{"role": "user", "content": "سلام"}])
        assert len(seen) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("provider", ["avalai", "custom"])
@pytest.mark.asyncio
async def test_ambient_openai_key_never_authenticates_another_provider(provider, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    kwargs = {"llm_base_url": "https://custom.invalid/v1"} if provider == "custom" else {}
    attempts = []
    client = LLMClient(Settings(_env_file=None, llm_provider=provider, llm_min_interval_seconds=0, **kwargs),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: attempts.append(request))))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([{"role": "user", "content": "سلام"}])
        assert caught.value.code == "provider_not_configured"
        assert not attempts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_authentication_failure_does_not_fall_back_to_another_provider():
    attempts = []
    def handle(request):
        attempts.append(request)
        return httpx.Response(401, json={"error": {"message": "private mock-credential", "code": "invalid_api_key"}})
    client = LLMClient(Settings(_env_file=None, llm_provider="openai", llm_api_key="mock-credential", llm_min_interval_seconds=0),
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        with pytest.raises(ProviderError) as caught:
            await client.complete([{"role": "user", "content": "سلام"}])
        assert caught.value.code == "provider_authentication_failed"
        assert "mock-credential" not in str(caught.value)
        assert len(attempts) == 1
        assert attempts[0].url.host == "api.openai.com"
    finally:
        await client.close()
