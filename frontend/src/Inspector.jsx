import React, { useMemo, useState } from 'react'
import { API, originalLink, pretty, request } from './api.js'
import { executionNodes, formatDuration, retrievalContext } from './inspection.js'
import { Icon } from './Icons.jsx'

export function JsonBlock({ value, label, className = '' }) {
  if (value === undefined || value === null) return null
  return <section className={`data-block ${className}`}>
    {label && <div className="field-label">{label}</div>}
    <pre dir="auto">{typeof value === 'string' ? value : pretty(value)}</pre>
  </section>
}

function Empty({ children = 'No payload was recorded for this tab.' }) { return <div className="empty-data">{children}</div> }

export function ResultTable({ result }) {
  const columns = result.columns || (result.rows?.[0] && !Array.isArray(result.rows[0]) ? Object.keys(result.rows[0]) : [])
  const rows = result.rows || []
  return <div className="result-table-wrap">
    <div className="table-caption"><span>{result.row_count ?? rows.length} rows{result.truncated ? ' · truncated by server' : ''}</span><span>{formatDuration(result.duration_ms)}</span></div>
    {columns.length ? <div className="table-scroll"><table><thead><tr>{columns.map((key, i) => <th key={`${key}-${i}`}>{key}</th>)}</tr></thead><tbody>{rows.slice(0, 50).map((row, i) => <tr key={i}>{columns.map((key, j) => <td key={`${key}-${j}`} dir="auto">{(Array.isArray(row) ? row[j] : row[key]) === null ? <span className="null-value">NULL</span> : typeof (Array.isArray(row) ? row[j] : row[key]) === 'object' ? pretty(Array.isArray(row) ? row[j] : row[key]) : String((Array.isArray(row) ? row[j] : row[key]) ?? '')}</td>)}</tr>)}</tbody></table></div> : <Empty>No result columns were returned.</Empty>}
    {rows.length > 50 && <div className="table-caption">Showing the first 50 returned rows. The complete recorded payload remains in Outputs.</div>}
    {result.evidence_warning && <div className="technical-warning">{result.evidence_warning}</div>}
  </div>
}

function SourceCards({ sources = [] }) {
  if (!sources.length) return <Empty>No PDF evidence is attached to this node.</Empty>
  return <div className="source-grid">{sources.map((source, i) => <article className="source-card" key={source.chunk_id || source.source_id || i}>
    <div className="source-heading"><Icon name="file"/><strong dir="auto">{source.title || source.document_id || 'Source document'}</strong><span>p. {source.page ?? '—'}</span></div>
    <div className="source-meta"><span>{source.source_id || source.section}</span>{source.score != null && <span>score {Number(source.score).toFixed(5)}</span>}<span>{source.source}</span></div>
    {source.text && <div className="evidence-text" dir="auto">{source.text}</div>}
    <div className="source-links">
      {source.filename && <a href={`${API}/documents/${encodeURIComponent(source.filename)}#page=${Number(source.page) || 1}`} target="_blank" rel="noreferrer"><Icon name="file" size={14}/> PDF page</a>}
      {originalLink(source.source_url) && <a href={originalLink(source.source_url)} target="_blank" rel="noreferrer"><Icon name="external" size={14}/> Original source</a>}
    </div>
    <details className="raw-details"><summary>Provenance</summary><JsonBlock value={Object.fromEntries(Object.entries(source).filter(([key]) => !['text', 'embedding_text'].includes(key)))}/></details>
  </article>)}</div>
}

function StructuredSources({ results = [] }) {
  const receipts = results.flatMap((result, query) => (result.record_sources || []).map(source => ({ ...source, query: query + 1 })))
  const unique = [...new Map(receipts.map(source => [`${source.id}:${source.source_url}`, source])).values()]
  if (!unique.length) return <Empty>No structured source receipts were recorded for this node.</Empty>
  return <div className="source-grid">{unique.map((source, i) => <article className="source-card" key={source.id || i}>
    <div className="source-heading"><Icon name="database"/><strong>{source.source || 'Structured source'}</strong><span>Query {source.query}</span></div>
    <div className="source-meta"><span>{source.id}</span><span>Observed {source.observed_at || 'not recorded'}</span></div>
    {source.name_fa && <div className="evidence-text" dir="auto">{source.name_fa}</div>}
    <div className="source-links">{originalLink(source.source_url) && <a href={originalLink(source.source_url)} target="_blank" rel="noreferrer"><Icon name="external" size={14}/> Original source</a>}</div>
    <details className="raw-details"><summary>Source receipt</summary><JsonBlock value={source}/></details>
  </article>)}</div>
}

function ModelInputs({ calls }) {
  return calls.map((call, index) => <section className="model-call" key={call.call_id || call.id || index}>
    <div className="section-heading"><h4>Model call {index + 1}</h4><span>{call.model || call.operation || call.stage || call.purpose}</span></div>
    {(call.messages || call.input?.messages || call.request?.messages)?.map((message, i) => <div className="prompt-message" key={i}>
      <div className="field-label">{message.role || 'message'}{message.name ? ` · ${message.name}` : ''}</div>
      <pre dir="auto">{typeof message.content === 'string' ? message.content : pretty(message.content)}</pre>
      {message.tool_calls && <JsonBlock value={message.tool_calls} label="Tool calls"/>}
    </div>)}
    {!(call.messages || call.input?.messages || call.request?.messages)?.length && <JsonBlock value={call.input || call.request || call.context} label="Recorded input"/>}
    {(call.tools || call.response_format) && <JsonBlock value={{ tools: call.tools, response_format: call.response_format }} label="Tool and response contracts"/>}
  </section>)
}

function RetrievalComparison({ context, disabled }) {
  const [busy, setBusy] = useState(false)
  const [includeRerank, setIncludeRerank] = useState(false)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  async function compare() {
    setBusy(true); setError(null)
    try { setData(await request(`/retrieval/compare?include_rerank=${includeRerank}`, { method: 'POST', body: JSON.stringify({ query: context.query, filters: context.filters, limit: 4 }) })) }
    catch (e) { setError({ code: e.code, message: e.message }) }
    finally { setBusy(false) }
  }
  return <section className="diagnostic-box">
    <div className="section-heading"><h4>Independent retrieval diagnostic</h4><span>New request</span></div>
    <p>This comparison runs only when requested here. It was not part of the recorded conversation turn.</p>
    <div className="diagnostic-controls"><label><input type="checkbox" checked={includeRerank} onChange={e => setIncludeRerank(e.target.checked)} disabled={busy || disabled}/> Include model reranking</label><button className="button small" onClick={compare} disabled={!context.query || busy || disabled}>{busy ? <span className="spinner"/> : <Icon name="search" size={15}/>}مقایسهٔ بازیابی</button></div>
    {error && <JsonBlock value={error} label="Diagnostic error"/>}
    {data && <div className="comparison-grid">{Object.entries(data.modes || {}).map(([mode, payload]) => <div className="comparison-mode" key={mode}><h4>{mode}</h4><JsonBlock value={payload.diagnostics} label="Diagnostics"/>{(payload.results || []).map((item, i) => <div className="comparison-hit" key={item.chunk_id || i}><span>#{i + 1} · {Number(item.score || 0).toFixed(5)}</span><strong dir="auto">{item.title}</strong><small>{item.document_id} · p. {item.page} · {item.section}</small></div>)}</div>)}</div>}
  </section>
}

export default function Inspector({ result = {}, events = [], calls = [], question = '', inFlight = false }) {
  const nodes = useMemo(() => executionNodes(result, events, calls), [result, events, calls])
  const [selected, setSelected] = useState(null)
  const [tab, setTab] = useState('Inputs')
  const node = nodes.find(item => item.key === selected) || nodes[0]
  const isSql = ['database_specialist', 'sql_guard'].includes(node?.key)
  const isRetrieval = node?.key === 'document_specialist'
  const isCalculator = node?.key === 'calculator'
  const context = isRetrieval ? retrievalContext(node, result) : null
  const sources = result.retrieved_sources || result.retrieval_evidence || result.sources || []
  return <div className="inspector" dir="ltr">
    <div className="inspector-heading"><div><span className="eyebrow">REQUEST EVIDENCE</span><h3>Execution inspector</h3></div><div className="request-meta"><span>{result.request_id || 'Streaming request'}</span>{formatDuration(result.metrics?.duration_ms) && <b>{formatDuration(result.metrics.duration_ms)}</b>}</div></div>
    {!nodes.length ? <Empty>Waiting for the first execution event.</Empty> : <>
      <div className="workflow-strip" aria-label="Recorded workflow nodes">{nodes.map((item, index) => <React.Fragment key={item.key}>{index > 0 && <span className="workflow-edge"><Icon name="chevron" size={14}/></span>}<button className={`workflow-node ${node?.key === item.key ? 'selected' : ''} state-${item.status}`} onClick={() => { setSelected(item.key); setTab('Inputs') }}><span className="node-icon"><Icon name={item.icon}/></span><span className="node-title">{item.title}</span><span className="node-state"><i className={`status-dot ${item.status}`}/>{item.status}{formatDuration(item.duration_ms) && <em>{formatDuration(item.duration_ms)}</em>}</span></button></React.Fragment>)}</div>
      <div className="workflow-caption">Observed components only · select a node to inspect its recorded evidence</div>
      <div className="inspector-tabs" role="tablist">{['Inputs', 'Outputs', 'Metrics', 'Sources'].map(label => <button role="tab" aria-selected={tab === label} key={label} onClick={() => setTab(label)} className={tab === label ? 'active' : ''}>{label}</button>)}</div>
      <div className="inspector-content">
        {tab === 'Inputs' && <>
          {node.key === 'coordinator' && <JsonBlock label="User question" value={question}/>}
          {node.calls.length > 0 && <ModelInputs calls={node.calls}/>}
          {isSql && result.sql_results?.map((item, index) => <JsonBlock value={item.sql} key={index} label={`Executed SQL ${index + 1}`} className="sql-code"/>)}
          {isRetrieval && <JsonBlock value={context} label="Executed retrieval request"/>}
          {isCalculator && result.calculations?.map((item, index) => <JsonBlock key={index} value={{ expression: item.expression, arguments: item.arguments || item.inputs, unit: item.unit, tool_call_id: item.tool_call_id }} label={`Native tool input ${index + 1}`}/>)}
          {node.events.some(item => Object.keys(item.data || {}).length) && <JsonBlock value={node.events.filter(item => item.status === 'started' || item.status === 'repair').map(item => ({ status: item.status, ...item.data }))} label="Recorded stage inputs"/>}
          {!node.calls.length && !isSql && !isRetrieval && !isCalculator && node.key !== 'coordinator' && <JsonBlock value={node.events.map(item => item.data)} label="Recorded stage data"/>}
        </>}
        {tab === 'Outputs' && <>
          {isSql && result.sql_results?.map((item, index) => <section className="data-block" key={index}><h4>Query {index + 1}</h4><ResultTable result={item}/></section>)}
          {isCalculator && result.calculations?.map((item, index) => <JsonBlock key={index} value={item} label={`Verified native result ${index + 1}`}/>)}
          {node.calls.map((call, index) => <JsonBlock key={call.call_id || index} value={call.output ?? call.response ?? call.result ?? call.message ?? call.error} label={`Model output ${index + 1}`}/>)}
          <JsonBlock value={node.events.filter(item => item.status !== 'started').map(item => ({ status: item.status, message: item.message, data: item.data }))} label="Execution events"/>
          {node.key === 'synthesis' && <JsonBlock value={{ status: result.status, answer: result.answer, offered_options: result.offered_options }} label="Final response"/>}
          {result.error && <JsonBlock value={result.error} label="Controlled error"/>}
        </>}
        {tab === 'Metrics' && <>
          <JsonBlock value={node.events.map(item => ({ status: item.status, duration_ms: item.duration_ms }))} label="Recorded stage timing"/>
          {node.calls.length > 0 && <JsonBlock value={node.calls.map(call => ({ model: call.model, status: call.status, duration_ms: call.duration_ms ?? call.latency_ms, usage: call.usage, attempt: call.attempt, error: call.error }))} label="Model usage and retries"/>}
          <JsonBlock value={isRetrieval ? result.metrics?.retrieval : result.metrics} label={isRetrieval ? 'Retrieval diagnostics' : 'Request metrics'}/>
          {result.calendar_context && <JsonBlock value={result.calendar_context} label="Calendar context"/>}
          {isRetrieval && <RetrievalComparison context={context} disabled={inFlight}/>}
        </>}
        {tab === 'Sources' && <>
          {isSql ? <StructuredSources results={result.sql_results}/> : <SourceCards sources={sources}/>}
          {isRetrieval && <details className="raw-details"><summary>Retrieved chunk references</summary><JsonBlock value={node.events.flatMap(item => item.data?.chunks || [])}/></details>}
        </>}
      </div>
    </>}
  </div>
}
