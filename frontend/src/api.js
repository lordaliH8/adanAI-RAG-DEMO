export const API = '/api'

export class ApiError extends Error {
  constructor(message, status = 0, code = 'request_failed') {
    super(message)
    this.status = status
    this.code = code
  }
}

export async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = body.error || body.detail || body
    throw new ApiError(typeof error === 'string' ? error : error.message || `Request failed (${response.status})`, response.status, error.code)
  }
  return body
}

// A POST stream cannot use EventSource. Decode incrementally so Persian code
// points and SSE frames remain intact across arbitrary network boundaries.
export async function consumeSSE(readable, onEvent) {
  const reader = readable.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  function drain(final = false) {
    buffer = buffer.replace(/\r\n/g, '\n')
    let boundary
    while ((boundary = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const lines = frame.split('\n')
      const eventName = lines.find(line => line.startsWith('event:'))?.slice(6).trim()
      const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
      if (!data) continue
      let parsed
      try { parsed = JSON.parse(data) } catch { throw new ApiError('The server returned an invalid stream frame.', 0, 'invalid_stream') }
      onEvent({ ...parsed, type: parsed.type || eventName || 'message' })
    }
    if (final && buffer.trim() && !buffer.trim().startsWith(':')) throw new ApiError('The stream ended before its final frame.', 0, 'incomplete_stream')
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      drain()
    }
    buffer += decoder.decode()
    drain(true)
  } finally { reader.releaseLock() }
}

export async function streamMessage(conversationId, question, turnKey, signal, onEvent) {
  const response = await fetch(`${API}/conversations/${encodeURIComponent(conversationId)}/messages/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': turnKey },
    body: JSON.stringify({ question }),
    signal,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const error = body.error || body.detail || body
    throw new ApiError(typeof error === 'string' ? error : error.message || `Request failed (${response.status})`, response.status, error.code)
  }
  if (!response.body) throw new ApiError('Streaming is unavailable.', 0, 'stream_unavailable')
  return consumeSSE(response.body, onEvent)
}

export function safeValue(value, depth = 0) {
  if (depth > 14) return '[depth limit]'
  if (Array.isArray(value)) return value.map(item => safeValue(item, depth + 1))
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [
    key, /authorization|api.?key|secret|password|access.?token/i.test(key) ? '[REDACTED]' : safeValue(item, depth + 1),
  ]))
  if (typeof value === 'string') return value.replace(/Bearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
  return value
}

export function pretty(value) { return JSON.stringify(safeValue(value), null, 2) }
export function originalLink(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : null } catch { return null }
}
