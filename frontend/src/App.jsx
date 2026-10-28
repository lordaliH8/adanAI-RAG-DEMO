import React, { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ApiError, request, streamMessage } from './api.js'
import { executionNodes, formatDuration } from './inspection.js'
import { Icon } from './Icons.jsx'
import Inspector from './Inspector.jsx'
import { scenarios } from './scenarios.js'

const uuid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
const dateLabel = value => {
  try { return new Intl.DateTimeFormat('fa-IR', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
  catch { return '' }
}
const humanAnswer = answer => String(answer || '')
  .replace(/\s*\[S\d+\]/g, '')
  .replace(/`?[a-f0-9]{24,64}`?/gi, 'رکورد منبع')

function ScenarioCard({ scenario, onChoose, disabled, compact = false }) {
  return <button className={`scenario-card ${compact ? 'compact' : ''}`} disabled={disabled} onClick={() => onChoose(scenario)}>
    <span className="scenario-icon"><Icon name={scenario.icon} size={20}/></span>
    <span className="scenario-copy"><span className="scenario-tag" dir="ltr">{scenario.tag}</span><strong>{scenario.title}</strong>{!compact && <span className="scenario-question">{scenario.question}</span>}</span>
    <Icon name="chevron" size={15} className="scenario-arrow"/>
  </button>
}

function Message({ message, busy, onRetry }) {
  const [details, setDetails] = useState(false)
  const user = message.role === 'user'
  const result = message.result || {}
  const events = message.liveEvents || []
  const calls = message.liveCalls || []
  const nodes = executionNodes(result, events, calls)
  const currentStage = nodes.at(-1)
  return <article className={`message-row ${user ? 'user' : 'assistant'}`}>
    <div className={`message-avatar ${user ? 'user-avatar' : ''}`}>{user ? <Icon name="chat" size={17}/> : <Icon name="compass" size={21}/>}</div>
    <div className="message-main">
      <div className="message-label">{user ? 'شما' : 'همسفر'}{message.pending && <span className="live-stage" dir="ltr"><span className="pulse-dot"/>{currentStage ? `${currentStage.title} · ${currentStage.status}` : 'Connecting'}</span>}</div>
      <div className={`message-bubble ${message.pending && !message.content ? 'awaiting' : ''}`} dir="rtl">
        {message.content ? <div className="markdown" dir="auto"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer"/>, pre: ({ node, ...props }) => <pre {...props} dir="ltr"/>, table: ({ node, ...props }) => <div className="answer-table"><table {...props}/></div> }}>{humanAnswer(message.content)}</ReactMarkdown></div> : <div className="thinking"><span/><span/><span/></div>}
      </div>
      {!user && <div className="message-actions">
        {(message.result || events.length > 0 || calls.length > 0) && <button className={`text-button details-toggle ${details ? 'selected' : ''}`} onClick={() => setDetails(!details)} aria-expanded={details}><Icon name="activity" size={14}/>جزئیات<Icon name="chevron" size={12} className={details ? 'rotate' : ''}/></button>}
        {message.retry && !busy && <button className="text-button" onClick={() => onRetry(message.retry)}><Icon name="refresh" size={13}/>تلاش دوباره</button>}
      </div>}
      {details && <Inspector result={result} events={events} calls={calls} question={message.question} inFlight={busy}/>}
    </div>
  </article>
}

export default function App() {
  const [config, setConfig] = useState(null)
  const [configError, setConfigError] = useState(null)
  const [conversations, setConversations] = useState([])
  const [conversationId, setConversationId] = useState(null)
  const [messages, setMessages] = useState([])
  const [view, setView] = useState('home')
  const [draft, setDraft] = useState('')
  const [activeScenario, setActiveScenario] = useState(null)
  const [busy, setBusy] = useState(false)
  const [opening, setOpening] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [provider, setProvider] = useState(null)
  const [testingProvider, setTestingProvider] = useState(false)
  const [resetJob, setResetJob] = useState(null)
  const [resetting, setResetting] = useState(false)
  const [notice, setNotice] = useState(null)
  const abortRef = useRef(null)
  const lockRef = useRef(false)
  const composerRef = useRef(null)
  const bottomRef = useRef(null)
  const mountedRef = useRef(true)
  const isResetting = resetting || config?.status === 'resetting'
  const controlsDisabled = busy || opening || isResetting

  async function refreshConfig() {
    try { const value = await request('/demo/config'); setConfig(value); setConfigError(null); return value }
    catch (e) { setConfigError(e); return null }
  }
  async function refreshHistory() {
    try { const value = await request('/conversations'); setConversations(value.conversations || []); return value }
    catch (e) { setNotice({ kind: 'error', text: 'تاریخچه گفتگوها دریافت نشد.', technical: e.message }) }
  }
  useEffect(() => {
    mountedRef.current = true
    refreshConfig(); refreshHistory()
    return () => { mountedRef.current = false; abortRef.current?.abort() }
  }, [])
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [messages.length, busy])
  useEffect(() => {
    if (config?.status !== 'resetting' || resetting) return
    const timer = setInterval(refreshConfig, 2500)
    return () => clearInterval(timer)
  }, [config?.status, resetting])
  useEffect(() => {
    const close = event => { if (event.key === 'Escape') setMenuOpen(false) }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [])

  async function openConversation(id) {
    if (controlsDisabled) return
    setOpening(true); setMenuOpen(false); setNotice(null)
    try {
      const data = await request(`/conversations/${encodeURIComponent(id)}`)
      setConversationId(id)
      setMessages((data.messages || []).map(item => ({ ...item, question: item.role === 'assistant' ? data.messages?.[data.messages.indexOf(item) - 1]?.content : undefined })))
      setView('conversation'); setActiveScenario(null); setDraft('')
    } catch (e) { setNotice({ kind: 'error', text: 'بازکردن گفتگو ممکن نشد.', technical: e.message }) }
    finally { setOpening(false) }
  }

  async function newConversation() {
    if (controlsDisabled) return
    setOpening(true); setMenuOpen(false); setNotice(null)
    try {
      const data = await request('/conversations', { method: 'POST', body: '{}' })
      setConversationId(data.id); setMessages([]); setDraft(''); setActiveScenario(null); setView('conversation')
      await refreshHistory()
      setTimeout(() => composerRef.current?.focus(), 30)
    } catch (e) { setNotice({ kind: 'error', text: 'گفتگوی جدید ساخته نشد.', technical: e.message }) }
    finally { setOpening(false) }
  }

  async function deleteConversation(id, event) {
    event.stopPropagation()
    if (controlsDisabled) return
    setOpening(true)
    try {
      await request(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' })
      if (conversationId === id) { setConversationId(null); setMessages([]); setView('new') }
      await refreshHistory()
    } catch (e) { setNotice({ kind: 'error', text: 'حذف گفتگو انجام نشد.', technical: e.message }) }
    finally { setOpening(false) }
  }

  function chooseScenario(scenario) {
    if (controlsDisabled) return
    setActiveScenario(scenario); setDraft(scenario.question); setMenuOpen(false)
    if (view !== 'conversation') { setConversationId(null); setMessages([]) }
    setView(conversationId && view === 'conversation' ? 'conversation' : 'new')
    setTimeout(() => { composerRef.current?.focus(); composerRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, 30)
  }

  async function sendMessage(retry = null) {
    const question = (retry?.question || draft).trim()
    if (!question || lockRef.current || isResetting || opening) return
    if (question.length > 2000) { setNotice({ kind: 'error', text: 'لطفاً پرسشتان را کوتاه‌تر از دو هزار نویسه بنویسید.' }); return }
    lockRef.current = true; setBusy(true); setNotice(null)
    const turnKey = retry?.key || uuid()
    const assistantId = retry?.assistantId || `pending-assistant-${turnKey}`
    const userId = retry?.userId || `pending-user-${turnKey}`
    const controller = new AbortController(); abortRef.current = controller
    const events = [], calls = []
    let id = retry?.conversationId || conversationId
    let completed = false
    let streamError = null
    try {
      if (!id) {
        const data = await request('/conversations', { method: 'POST', body: '{}' })
        id = data.id; setConversationId(id)
      }
      setView('conversation'); setMenuOpen(false); setDraft('')
      const pending = { id: assistantId, role: 'assistant', content: '', pending: true, question, liveEvents: [], liveCalls: [] }
      if (retry) setMessages(items => items.map(item => item.id === assistantId ? pending : item))
      else setMessages(items => [...items, { id: userId, role: 'user', content: question }, pending])
      await streamMessage(id, question, turnKey, controller.signal, event => {
        const payload = ['trace', 'provider_retry'].includes(event.type)
          ? (event.step ? event : event.data?.step ? event.data : event)
          : event.call || event.data || event
        if (event.type === 'trace' || event.type === 'provider_retry') {
          events.push(event.type === 'provider_retry' ? { ...payload, step: payload.step || 'llm', status: payload.status || 'retry' } : payload)
          setMessages(items => items.map(item => item.id === assistantId ? { ...item, liveEvents: [...events] } : item))
        } else if (event.type === 'model_call') {
          calls.push(payload)
          setMessages(items => items.map(item => item.id === assistantId ? { ...item, liveCalls: [...calls] } : item))
        } else if (event.type === 'result') {
          completed = true
          const result = { ...payload, trace: payload.trace || [...events], model_calls: payload.model_calls || [...calls] }
          setMessages(items => items.map(item => item.id === userId ? { ...item, id: payload.user_message_id || userId } : item.id === assistantId ? { id: payload.assistant_message_id || assistantId, role: 'assistant', content: result.answer || 'پاسخی دریافت نشد.', result, question, pending: false } : item))
        } else if (event.type === 'error') streamError = payload.error || payload
      })
      if (streamError) throw new ApiError(streamError.message || 'The request did not complete.', streamError.status || 0, streamError.code || 'stream_error')
      if (!completed) throw new ApiError('The connection ended before a final result arrived.', 0, 'incomplete_stream')
    } catch (e) {
      const cancelled = e.name === 'AbortError'
      const retryInfo = !cancelled && !e.status ? { question, conversationId: id, key: turnKey, userId, assistantId } : null
      setMessages(items => {
        const replacement = { id: assistantId, role: 'assistant', content: cancelled ? 'درخواست متوقف شد.' : 'دریافت پاسخ کامل نشد. می‌توانید جزئیات خطا را بررسی کنید و دوباره تلاش کنید.', result: { status: cancelled ? 'cancelled' : 'error', error: { code: cancelled ? 'client_cancelled' : e.code, message: cancelled ? 'Cancelled by the user.' : e.message }, trace: [...events], model_calls: [...calls] }, question, pending: false, retry: retryInfo }
        return items.some(item => item.id === assistantId) ? items.map(item => item.id === assistantId ? replacement : item) : [...items, replacement]
      })
    } finally {
      lockRef.current = false; abortRef.current = null
      if (mountedRef.current) { setBusy(false); refreshHistory() }
    }
  }

  async function testProvider() {
    if (testingProvider || controlsDisabled) return
    setTestingProvider(true); setProvider(null)
    try { setProvider(await request('/demo/provider-test', { method: 'POST', body: '{}' })) }
    catch (e) { setProvider({ status: 'failed', error: { code: e.code, message: e.message } }) }
    finally { setTestingProvider(false) }
  }

  async function resetData() {
    if (controlsDisabled || testingProvider) return
    setResetting(true); setResetJob({ status: 'starting', events: [] }); setNotice(null)
    try {
      const started = await request('/demo/reset', { method: 'POST', body: '{}' })
      let job
      for (let attempt = 0; attempt < 450; attempt++) {
        job = await request(`/demo/jobs/${encodeURIComponent(started.job_id)}`)
        if (!mountedRef.current) return
        setResetJob(job)
        if (['completed', 'complete', 'succeeded', 'failed', 'error'].includes(job.status)) break
        await new Promise(resolve => setTimeout(resolve, 800))
      }
      if (!job || !['completed', 'complete', 'succeeded', 'failed', 'error'].includes(job.status)) throw new ApiError('The reset is still running. Refresh system status to check progress.', 0, 'reset_pending')
      if (['completed', 'complete', 'succeeded'].includes(job.status)) {
        setConversationId(null); setMessages([]); setDraft(''); setActiveScenario(null); setView(current => current === 'home' ? 'home' : 'new')
      }
      await refreshConfig(); await refreshHistory()
    } catch (e) { setResetJob(previous => ({ ...previous, status: 'failed', error: { code: e.code, message: e.message } })) }
    finally { if (mountedRef.current) setResetting(false) }
  }

  const showConversation = view === 'conversation' && messages.length > 0
  const simulationLabel = config?.simulation_date_fa || config?.simulation_date || '2026-11-16'
  const followups = messages.at(-1)?.role === 'assistant' && !busy ? (messages.at(-1)?.result?.follow_up_suggestions || activeScenario?.followups || []) : []

  const statusPanels = <>
        {(configError || notice) && <div className="notice error"><Icon name="activity"/><div><strong>{notice?.text || 'ارتباط با سرویس برقرار نشد.'}</strong><span dir="ltr">{notice?.technical || configError?.message}</span></div><button className="icon-button" onClick={() => { setNotice(null); if (configError) refreshConfig() }} aria-label="بستن پیام"><Icon name="close" size={15}/></button></div>}
        {provider && <div className={`notice provider-notice ${provider.status === 'passed' ? 'success' : 'error'}`} dir="ltr"><Icon name={provider.status === 'passed' ? 'check' : 'activity'}/><div><strong>{provider.status === 'passed' ? 'Model connection verified' : 'Model connection failed'}</strong><span>{provider.model || config?.model}{formatDuration(provider.latency_ms) ? ` · ${formatDuration(provider.latency_ms)}` : ''}{provider.error ? ` · ${provider.error.code || ''}: ${provider.error.message || ''}` : ''}</span></div><button className="icon-button" onClick={() => setProvider(null)} aria-label="بستن نتیجه آزمون"><Icon name="close" size={15}/></button></div>}
        {resetJob && <section className="reset-panel" dir="ltr"><div className="section-heading"><h3>{resetting ? 'Rebuilding the data pipeline' : `Data reset · ${resetJob.status}`}</h3>{resetting ? <span className="spinner"/> : <button className="icon-button" onClick={() => setResetJob(null)} aria-label="بستن گزارش بازنشانی"><Icon name="close" size={15}/></button>}</div><div className="reset-events">{(resetJob.events || []).map((event, i) => <div key={i}><i className={`status-dot ${event.status}`}/><b>{String(event.stage || '').replaceAll('_', ' ')}</b><span>{event.message}</span></div>)}</div>{resetJob.error && <pre>{JSON.stringify(resetJob.error, null, 2)}</pre>}</section>}
  </>

  if (view === 'home') return <div className="home-screen" dir="rtl">
    <main className="home-canvas">
      <div className="home-mark" aria-hidden="true"><Icon name="compass" size={46}/></div>
      <h1>دموی دستیار هوشمند همسفر</h1>
      <div className="home-clock">
        <span dir="ltr">SIMULATION DATE · FIXED</span>
        <strong>{simulationLabel}</strong>
        <small dir="ltr">{config?.simulation_date || '2026-11-16'}{config?.simulation_calendar === 'jalali' ? ' · Jalali' : ''}</small>
      </div>
      <div className="home-actions">
        <button className="button primary" disabled={controlsDisabled} onClick={() => { setView('new'); setConversationId(null); setMessages([]); setDraft(''); setActiveScenario(null) }}><Icon name="chat" size={18}/>شروع مکالمه</button>
        <button className="button" onClick={testProvider} disabled={testingProvider || controlsDisabled}>{testingProvider ? <span className="spinner"/> : <Icon name="activity" size={18}/>}آزمون مدل</button>
        <button className="button" onClick={resetData} disabled={controlsDisabled || testingProvider} title="بازسازی داده‌ها و پاک‌کردن گفتگوها">{isResetting ? <span className="spinner"/> : <Icon name="refresh" size={18}/>}بازنشانی داده</button>
        <a className="button" href="/api/docs" target="_blank" rel="noreferrer"><Icon name="code" size={18}/>مستندات API</a>
      </div>
      <div className="home-status">{statusPanels}</div>
    </main>
  </div>

  return <div className="app-shell">
    {menuOpen && <button className="mobile-backdrop" aria-label="بستن فهرست" onClick={() => setMenuOpen(false)}/>}
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`} dir="ltr">
      <button className="sidebar-brand" onClick={() => { if (!controlsDisabled) { setView('home'); setConversationId(null); setMessages([]); setDraft(''); setMenuOpen(false) } }}><span className="brand-mark"><Icon name="compass" size={28}/></span><span><strong>همسفر</strong><small>TRAVEL INTELLIGENCE</small></span></button>
      <div className="nav-label">CONVERSATION HUB</div>
      <nav>
        <button className={`nav-item ${view === 'history' ? 'active' : ''}`} disabled={controlsDisabled} onClick={() => { setView('history'); setMenuOpen(false); refreshHistory() }}><Icon name="history"/><span>History</span><b>{conversations.length}</b></button>
        <button className={`nav-item ${['new', 'conversation'].includes(view) ? 'active' : ''}`} disabled={controlsDisabled} onClick={newConversation}><Icon name="plus"/><span>New conversation</span></button>
        <button className={`nav-item ${view === 'scenarios' ? 'active' : ''}`} disabled={controlsDisabled} onClick={() => { setView('scenarios'); setMenuOpen(false) }}><Icon name="beaker"/><span>Testing scenarios</span></button>
      </nav>
      <div className="recent-heading"><span className="nav-label">RECENT CONVERSATIONS</span><button className="icon-button" title="بازخوانی تاریخچه" aria-label="بازخوانی تاریخچه" onClick={refreshHistory} disabled={controlsDisabled}><Icon name="refresh" size={13}/></button></div>
      <div className="recent-list">{conversations.slice(0, 9).map(item => <button key={item.id} className={`recent-item ${conversationId === item.id ? 'active' : ''}`} disabled={controlsDisabled} onClick={() => openConversation(item.id)}><span dir="rtl">{item.title || 'گفتگوی تازه'}</span><small>{dateLabel(item.updated_at || item.created_at)}</small></button>)}{!conversations.length && <div className="sidebar-empty">Your conversations will appear here.</div>}</div>
      <div className="sidebar-bottom"><span className={`connection-dot ${config && !configError ? 'connected' : ''}`}/><div><strong>{configError ? 'Connection unavailable' : config?.status === 'resetting' ? 'Preparing data' : config ? 'System ready' : 'Connecting'}</strong><span>{config?.model || 'FastAPI · Qdrant · SQLite'}</span></div><button className="icon-button" aria-label="بازخوانی وضعیت" title="بازخوانی وضعیت" onClick={refreshConfig}><Icon name="refresh" size={15}/></button></div>
    </aside>

    <div className="workspace">
      <header className="topbar">
        <div className="topbar-title"><button className="icon-button mobile-menu" onClick={() => setMenuOpen(true)} aria-label="بازکردن فهرست"><Icon name="menu"/></button><span className="eyebrow">HAMSAFAR / INTERACTIVE DEMO</span></div>

      </header>

      <main className={`main-content ${showConversation ? 'with-conversation' : ''}`}>
        {statusPanels}

        {view === 'history' ? <section className="history-page"><div className="section-heading"><div><span className="eyebrow">SAVED ON THE SERVER</span><h2>گفتگوهای شما</h2></div><button className="button small" onClick={newConversation} disabled={controlsDisabled}><Icon name="plus" size={15}/>گفتگوی جدید</button></div><div className="history-grid">{conversations.map(item => <article className="history-card" key={item.id}><button className="history-open" disabled={controlsDisabled} onClick={() => openConversation(item.id)}><Icon name="chat"/><strong>{item.title || 'گفتگوی تازه'}</strong><span>{dateLabel(item.updated_at || item.created_at)}</span><small>{item.message_count ?? 0} messages</small></button><button className="icon-button delete-conversation" disabled={controlsDisabled} onClick={event => deleteConversation(item.id, event)} aria-label="حذف گفتگو" title="حذف گفتگو"><Icon name="trash" size={16}/></button></article>)}</div>{!conversations.length && <div className="empty-history"><Icon name="chat" size={36}/><h3>هنوز گفتگویی ندارید</h3><p>اولین سفر را با یک سؤال شروع کنید.</p><button className="button primary" onClick={newConversation}>شروع گفتگو</button></div>}</section> : view === 'scenarios' ? <section className="scenarios-page"><div className="section-heading"><div><span className="eyebrow">EXPLORE THE DEMO</span><h2>چند راه برای شروع گفتگو</h2><p>یک پیشنهاد را انتخاب کنید و سؤال را با جزئیات سفر خودتان تغییر دهید.</p></div><span className="count-pill">{scenarios.length} starters</span></div><div className="scenario-grid">{scenarios.map(scenario => <ScenarioCard scenario={scenario} key={scenario.title} onChoose={chooseScenario} disabled={controlsDisabled}/>)}</div></section> : <>
          {!showConversation && <div className="empty-chat" dir="ltr"><Icon name="compass" size={29}/><span>Start a conversation below.</span></div>}
          {showConversation && <section className="conversation" aria-live="polite">{messages.map(message => <Message key={message.id} message={message} busy={busy} onRetry={sendMessage}/>)}{followups.length > 0 && <div className="followups"><span>ادامهٔ گفتگو</span>{followups.slice(0, 3).map((question, i) => <button key={i} disabled={controlsDisabled} onClick={() => { setDraft(question); composerRef.current?.focus() }}>{question}<Icon name="chevron" size={12}/></button>)}</div>}<div className="conversation-end" ref={bottomRef}/></section>}
        </>}
      </main>

      {view !== 'history' && view !== 'scenarios' && <div className="composer-dock"><form className={`composer ${busy ? 'is-busy' : ''}`} onSubmit={event => { event.preventDefault(); sendMessage() }}><textarea ref={composerRef} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); sendMessage() } }} placeholder="دربارهٔ سفر، مقصد یا بودجه‌ات بنویس…" rows={2} disabled={controlsDisabled} maxLength={2100} dir="auto" aria-label="پرسش شما"/><div className="composer-bottom"><div className="composer-hint" dir="ltr">{busy ? <><span className="pulse-dot"/>Live execution · answer follows evidence checks</> : <><span className="key-hint">↵</span> to send · Shift + Enter for a new line</>}</div>{busy ? <button type="button" className="button stop-button" onClick={() => abortRef.current?.abort()}><Icon name="stop" size={15}/>توقف</button> : <button type="submit" className="button primary send-button" disabled={!draft.trim() || controlsDisabled}><span>ارسال</span><Icon name="send" size={16}/></button>}</div></form><div className="composer-footnote" dir="ltr">Source-backed travel snapshot · this demo does not make reservations</div></div>}
    </div>
  </div>
}
