import { test } from 'node:test'
import assert from 'node:assert/strict'
import { consumeSSE, safeValue } from './api.js'
import { executionNodes } from './inspection.js'

test('SSE preserves Persian across single-byte network chunks and ignores heartbeats', async () => {
  const text = ': heartbeat\n\nevent: trace\ndata: {"type":"trace","step":"coordinator","message":"سلام همسفر"}\n\nevent: result\ndata: {"type":"result","data":{"answer":"شیراز"}}\n\n'
  const bytes = new TextEncoder().encode(text)
  const stream = new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close() } })
  const events = []
  await consumeSSE(stream, event => events.push(event))
  assert.equal(events[0].message, 'سلام همسفر')
  assert.equal(events[1].data.answer, 'شیراز')
  assert.equal(events.length, 2)
})

test('SSE rejects a truncated final response instead of treating it as success', async () => {
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('event: result\ndata: {"answer":')); controller.close() } })
  await assert.rejects(() => consumeSSE(stream, () => {}), /final frame/)
})

test('paired model-call trace events retain original prompts on their actual component', () => {
  const trace = [
    { step: 'coordinator', status: 'started' },
    { step: 'model_call', status: 'started', data: { call_id: 'call-one', model: 'demo', messages: [{ role: 'system', content: 'فقط بر اساس شواهد پاسخ بده.' }], response_format: { json_schema: { name: 'coordinator_plan' } } } },
    { step: 'model_call', status: 'completed', duration_ms: 120, data: { call_id: 'call-one', response: { answer: 'ثبت شد' }, usage: { prompt_tokens: 20 } } },
    { step: 'coordinator', status: 'completed' },
  ]
  const nodes = executionNodes({ trace })
  assert.deepEqual(nodes.map(node => node.key), ['coordinator'])
  assert.equal(nodes[0].calls.length, 1)
  assert.equal(nodes[0].calls[0].messages[0].content, 'فقط بر اساس شواهد پاسخ بده.')
  assert.equal(nodes[0].calls[0].usage.prompt_tokens, 20)
  assert.equal(nodes[0].calls[0].duration_ms, 120)
})

test('diagnostic rerank call appears only when the request actually recorded one', () => {
  assert.deepEqual(executionNodes({ trace: [{ step: 'synthesis', status: 'completed' }] }).map(node => node.key), ['synthesis'])
  const nodes = executionNodes({ trace: [{ step: 'document_specialist', status: 'completed' }], metrics: { retrieval: { rerank_call: { stage: 'document_specialist', purpose: 'retrieval_ranking', messages: [{ role: 'user', content: 'رتبه‌بندی کن' }], duration_ms: 10 } } } })
  assert.equal(nodes[0].calls[0].purpose, 'retrieval_ranking')
  assert.equal(nodes.length, 1)
})

test('a model call without a schema remains attached to its recorded synthesis stage', () => {
  const nodes = executionNodes({ trace: [
    { step: 'synthesis', status: 'started' },
    { step: 'model_call', status: 'started', data: { call_id: 'tool-call', messages: [{ role: 'system', content: 'در صورت نیاز ابزار محاسبه را فراخوانی کن.' }], tools: [{ type: 'function' }] } },
    { step: 'model_call', status: 'completed', data: { call_id: 'tool-call', response: { choices: [] } } },
    { step: 'synthesis', status: 'completed' },
  ] })
  assert.deepEqual(nodes.map(node => node.key), ['synthesis'])
  assert.equal(nodes[0].calls.length, 1)
  assert.equal(nodes[0].calls[0].tools.length, 1)
})

test('nested technical payload redaction does not change Persian prompt text', () => {
  assert.deepEqual(safeValue({ messages: [{ content: 'سفر فردا را بررسی کن.' }], api_key: 'private', nested: { authorization: 'Bearer private' } }), { messages: [{ content: 'سفر فردا را بررسی کن.' }], api_key: '[REDACTED]', nested: { authorization: '[REDACTED]' } })
})
