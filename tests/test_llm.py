"""No-network checks of retry policy, native tool preservation and embeddings."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage
from openai import RateLimitError

from app.llm import LLMClient, ProviderError, classify_error, retry_delay


def settings(**changes):
    values = dict(llm_api_key="fake-private-test-key", llm_base_url="https://provider.invalid/v1",
                  llm_model="test-model", llm_timeout_seconds=1, llm_max_retries=1,
                  llm_min_interval_seconds=0, embedding_model="test-embedding",
                  embedding_batch_size=2, embedding_dimensions=3, langfuse_enabled=False,
                  as_of="2026-10-27", max_history_messages=4)
    values.update(changes)
    return SimpleNamespace(**values)


class ChatStub:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_error(status, retry_after=None):
    response = httpx.Response(status, headers={"retry-after": retry_after} if retry_after else {},
                              request=httpx.Request("POST", "https://provider.invalid"))
    return httpx.HTTPStatusError("raw body contains fake-private-test-key", request=response.request, response=response)


class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def make_client(self, responses, **changes):
        stub = ChatStub(responses)
        with patch("app.llm.ChatOpenAI", return_value=stub):
            client = LLMClient(settings(**changes))
        self.addAsyncCleanup(client.close)
        return client, stub

    async def test_native_tool_ids_and_tool_result_protocol_survive_conversion(self):
        native = AIMessage(content="", tool_calls=[{"name": "calculate", "args": {"expression": "2+3", "precision": 0, "unit": "تومان"}, "id": "call-preserved", "type": "tool_call"}])
        client, stub = await self.make_client([native, AIMessage(content="پنج")])
        first = await client.complete([{"role": "user", "content": "جمع کن"}])
        tool = first["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool["id"], "call-preserved")
        self.assertEqual(tool["function"]["name"], "calculate")
        await client.complete([{"role": "user", "content": "جمع کن"},
                               first["choices"][0]["message"],
                               {"role": "tool", "tool_call_id": "call-preserved", "content": '{"value":"5"}'}])
        self.assertEqual(stub.calls[-1][0][-1].tool_call_id, "call-preserved")

    async def test_429_retries_once_and_honors_capped_retry_after(self):
        client, stub = await self.make_client([http_error(429, "99"), AIMessage(content="پاسخ")])
        events = []
        with patch("app.llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await client.complete([{"role": "user", "content": "سلام"}], emit=events.append)
        sleep.assert_awaited_once_with(30.0)
        self.assertEqual(len(stub.calls), 2)
        self.assertEqual(events[0]["code"], "provider_rate_limited")
        self.assertNotIn("fake-private-test-key", str(events))

    def test_quota_classification_reads_only_structured_codes_direct_or_nested(self):
        bodies = [
            {"code": "insufficient_quota", "message": "private balance fake-private-test-key"},
            {"error": {"code": "insufficient_quota", "message": "private balance fake-private-test-key"}},
            {"type": "insufficient_quota"},
            {"error": {"type": "insufficient_quota"}},
        ]
        for body in bodies:
            with self.subTest(body_keys=list(body)):
                response = httpx.Response(429, json=body, request=httpx.Request("POST", "https://provider.invalid"))
                error = RateLimitError("private balance fake-private-test-key", response=response, body=body)
                classified = classify_error(error)
                self.assertEqual(classified.code, "provider_credit_exhausted")
                self.assertFalse(classified.retryable)
                self.assertNotIn("fake-private-test-key", classified.message)
                self.assertNotIn("balance", classified.message)
        error = http_error(429)
        error.code = "insufficient_quota"
        self.assertEqual(classify_error(error).code, "provider_credit_exhausted")
        self.assertEqual(classify_error(http_error(429)).code, "provider_rate_limited")
        self.assertTrue(classify_error(http_error(429)).retryable)

    async def test_exhausted_credit_direct_and_nested_openai_errors_never_retry(self):
        for body in ({"code": "insufficient_quota"}, {"error": {"code": "insufficient_quota"}}):
            with self.subTest(body_keys=list(body)):
                response = httpx.Response(429, json=body, headers={"retry-after": "30"},
                    request=httpx.Request("POST", "https://provider.invalid"))
                error = RateLimitError("private balance fake-private-test-key", response=response, body=body)
                client, stub = await self.make_client([error], llm_max_retries=2)
                events = []
                with patch("app.llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
                    with self.assertRaises(ProviderError) as caught:
                        await client.complete([{"role": "user", "content": "سلام"}], emit=events.append)
                self.assertEqual(caught.exception.code, "provider_credit_exhausted")
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(stub.calls), 1)
                self.assertEqual(client.usage_snapshot()["retries"], 0)
                self.assertEqual(events, [])
                self.assertNotIn("fake-private-test-key", str(caught.exception))
                sleep.assert_not_awaited()

    async def test_embedding_http_quota_body_is_not_retried(self):
        client, _ = await self.make_client([], llm_max_retries=2)
        await client._http.aclose()
        calls = []
        def transport(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"type": "insufficient_quota", "message": "private credit data"}})
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        with patch("app.llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ProviderError) as caught:
                await client.embed(["متن آزمایشی"])
        self.assertEqual(caught.exception.code, "provider_credit_exhausted")
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.usage_snapshot()["retries"], 0)
        self.assertNotIn("private credit data", str(caught.exception))
        sleep.assert_not_awaited()

    async def test_authentication_error_is_not_retried_or_exposed(self):
        client, stub = await self.make_client([http_error(401)])
        with patch("app.llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ProviderError) as caught:
                await client.complete([{"role": "user", "content": "سلام"}])
        self.assertEqual(caught.exception.code, "provider_authentication_failed")
        self.assertNotIn("fake-private-test-key", str(caught.exception))
        self.assertEqual(len(stub.calls), 1)
        sleep.assert_not_awaited()

    async def test_timeouts_exhaust_a_bounded_budget(self):
        client, stub = await self.make_client([httpx.ReadTimeout("private body"), httpx.ReadTimeout("private body")])
        with patch("app.llm.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(ProviderError) as caught:
                await client.complete([{"role": "user", "content": "سلام"}])
        self.assertEqual(caught.exception.code, "provider_timeout")
        self.assertEqual(len(stub.calls), 2)
        self.assertNotIn("private body", str(caught.exception))

    async def test_embedding_batch_indices_are_reordered_and_dimensions_checked(self):
        client, _ = await self.make_client([])
        await client._http.aclose()
        calls = []

        def transport(request):
            import json
            payload = json.loads(request.content)
            calls.append(payload)
            data = [{"index": i, "embedding": [float(i), 1.0, 2.0]} for i in reversed(range(len(payload["input"])))]
            return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 3}})

        client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        output = await client.embed(["یک", "دو", "سه"])
        self.assertEqual([len(c["input"]) for c in calls], [2, 1])
        self.assertEqual(output, [[0.0, 1.0, 2.0], [1.0, 1.0, 2.0], [0.0, 1.0, 2.0]])

    async def test_bad_embedding_shape_is_controlled(self):
        client, _ = await self.make_client([])
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})))
        with self.assertRaises(ProviderError) as caught:
            await client.embed(["متن"])
        self.assertEqual(caught.exception.code, "invalid_embedding_response")

    def test_retry_after_clamped(self):
        self.assertEqual(retry_delay(http_error(429, "3600"), 0), 30)
        self.assertEqual(retry_delay(http_error(429, "-1"), 0), 0)


if __name__ == "__main__":
    unittest.main()
