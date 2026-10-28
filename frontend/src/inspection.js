const aliases = {
  coordinator: ['Coordinator', 'compass'],
  entity_linker: ['Entity linker', 'link'],
  database_specialist: ['Database', 'database'],
  sql_guard: ['SQL guard', 'shield'],
  document_specialist: ['Retrieval', 'search'],
  calculator: ['Calculator', 'calculator'],
  synthesis: ['Synthesis', 'sparkles'],
  source_validation: ['Source validation', 'check'],
  model_output: ['Output validation', 'check'],
  llm: ['Provider', 'activity'],
  workflow: ['Workflow', 'activity'],
}

export function callStage(call) {
  const stage = String(call.step || call.stage || call.purpose || call.name || call.response_format?.json_schema?.name || call.context?.stage || '')
  if (aliases[stage]) return stage
  if (/coordinator|planning/i.test(stage)) return 'coordinator'
  if (/entity/i.test(stage)) return 'entity_linker'
  if (/sql|database/i.test(stage)) return 'database_specialist'
  if (/retriev|document|rerank/i.test(stage)) return 'document_specialist'
  if (/calculat/i.test(stage)) return 'calculator'
  if (/synth|answer|citation/i.test(stage)) return 'synthesis'
  return stage || 'llm'
}

export function recordedModelCalls(events = [], extraCalls = []) {
  const calls = new Map()
  const unpaired = []
  let activeStage = null
  for (const event of events) {
    if (event.step !== 'model_call') {
      if (['coordinator', 'database_specialist', 'document_specialist', 'synthesis'].includes(event.step)) activeStage = event.step
      continue
    }
    const data = event.data || {}
    if (!data.call_id) { unpaired.push({ ...data, status: event.status, duration_ms: event.duration_ms }); continue }
    const previous = calls.get(data.call_id) || {}
    const call = { ...previous, ...data, status: event.status, duration_ms: event.duration_ms || previous.duration_ms }
    call.purpose = call.purpose || call.response_format?.json_schema?.name
    if (!call.stage) {
      const schemaStage = callStage({ purpose: call.purpose })
      call.stage = aliases[schemaStage] && schemaStage !== 'llm' ? schemaStage : activeStage || 'llm'
    }
    calls.set(data.call_id, call)
  }
  for (const call of extraCalls) {
    if (call.call_id) calls.set(call.call_id, { ...calls.get(call.call_id), ...call })
    else unpaired.push(call)
  }
  return [...calls.values(), ...unpaired]
}

export function executionNodes(result = {}, liveEvents = [], liveCalls = []) {
  const events = result.trace?.length ? result.trace : liveEvents
  const calls = recordedModelCalls(events, result.model_calls?.length ? result.model_calls : liveCalls)
  const rerank = result.metrics?.retrieval?.rerank_call
  if (rerank && !calls.some(call => call.purpose === rerank.purpose && call.duration_ms === rerank.duration_ms)) calls.push(rerank)
  const nodes = new Map()
  function ensure(step) {
    if (!nodes.has(step)) nodes.set(step, { key: step, title: aliases[step]?.[0] || step.replaceAll('_', ' '), icon: aliases[step]?.[1] || 'activity', events: [], calls: [] })
    return nodes.get(step)
  }
  for (const event of events || []) if (event.step && event.step !== 'model_call') ensure(event.step).events.push(event)
  for (const call of calls || []) ensure(callStage(call)).calls.push(call)
  return [...nodes.values()].map(node => {
    const last = node.events.at(-1) || node.calls.at(-1) || {}
    const duration = [...node.events, ...node.calls].map(item => Number(item.duration_ms || item.latency_ms || 0)).filter(Number.isFinite)
    return { ...node, status: last.status || (node.calls.length ? 'completed' : 'unknown'), duration_ms: Math.max(0, ...duration) }
  })
}

export function retrievalContext(node, result = {}) {
  const completed = [...node.events].reverse().find(event => event.data?.query)
  return {
    query: completed?.data?.query || '',
    mode: completed?.data?.mode || result.metrics?.retrieval?.mode,
    filters: completed?.data?.filters || {},
  }
}

export function formatDuration(value) {
  const amount = Number(value)
  if (!Number.isFinite(amount) || amount <= 0) return null
  return amount >= 1000 ? `${(amount / 1000).toFixed(1)} s` : `${Math.round(amount)} ms`
}
