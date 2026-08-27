import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, ArrowDownToLine, ArrowLeft, ArrowRight, BellRing, BookOpenCheck, CalendarClock, Check,
  CheckCircle2, ChevronDown, ChevronRight, CircleAlert, Database,
  CircleDot, FileArchive, FileCheck2, FileSearch, FileStack, FileText, Fingerprint,
  ExternalLink, Globe2, HardDrive, Info, Languages, Layers3, LayoutDashboard, Library, LockKeyhole, MapPin, Menu,
  MessageSquareText, Network, PanelLeftClose, RefreshCw, Search, Send, Server,
  Share2, SlidersHorizontal, Target, TestTube2, UploadCloud,
  UserRound, Users, X,
} from 'lucide-react'
import { api } from './api.js'

const NAV_GROUPS = [
  { label: '运行总览', items: [{ id: 'overview', label: '系统概览', icon: LayoutDashboard }] },
  { label: '监测与数据', items: [
    { id: 'targets', label: '监测对象', icon: Users },
    { id: 'collection', label: '采集与批次', icon: Database },
    { id: 'topics', label: '重点专题', icon: Target },
  ] },
  { label: '知识与研判', items: [
    { id: 'knowledge', label: '嵌入式知识库', icon: Library },
    { id: 'graph', label: '知识图谱', icon: Share2 },
    { id: 'analysis', label: '分析研判', icon: SlidersHorizontal },
    { id: 'alerts', label: '风险线索', icon: BellRing },
  ] },
  { label: '分类监测', items: [
    { id: 'objects', label: '对象分层', icon: Users },
    { id: 'opinion', label: '舆情分析', icon: SlidersHorizontal },
  ] },
  { label: '输出与治理', items: [
    { id: 'reports', label: '报告中心', icon: FileText },
    { id: 'chat', label: '检索式问答', icon: MessageSquareText },
  ] },
]

const ROUTE_META = Object.fromEntries(NAV_GROUPS.flatMap((group) => group.items).map((item) => [item.id, item]))

const CATEGORY_LABELS = {
  account: '账号实体', actor: '人物实体', profile_signal: '画像信号', content: '逐帖内容',
  event: '事件线索', relationship_layer: '关系圈层', business_signal: '商业与政治信号',
  source: '来源台账', analysis: '研究分析', quality_conflict: '口径冲突', production_gap: '生产缺口',
}

const TASK_DIMENSION_LABELS = { person: '人物', account: '账号', keyword: '关键词', hashtag: '话题标签' }
const FREQUENCY_LABELS = { '15m': '每 15 分钟', '30m': '每 30 分钟', '1h': '每小时', '6h': '每 6 小时', daily: '每天' }
const MEDIA_LABELS = { text: '文本', image: '图片', video: '视频' }
const LANGUAGE_LABELS = { zh: '中文', en: '英文', es: '西班牙语', fr: '法语' }

const EVIDENCE_LABELS = {
  explicit_source_text: '源文件明示', direct_post_excerpt: '直接帖子摘录', metric_only_no_text: '仅有指标、无正文',
  reported_by_source_file: '来源报告转述', reported_by_source_file_plus_speculation: '转述与推测混合',
  source_list_only: '来源清单', link_domain_only: '来源域名线索', source_analysis: '源文件作者分析',
  explicit_source_text_plus_analysis: '明示与分析混合', inference_or_speculation: '研究推断',
  mixed_reported_and_inferred: '转述与推断混合', source_conflict: '跨文件口径冲突',
  production_gap: '生产字段缺口', source_snapshot: '来源快照',
}

const GRAPH_VIEWS = [
  { id: 'actors', label: '人物与账号', description: '人物、账号和主题的归属关系' },
  { id: 'events', label: '事件证据', description: '人物、事件与来源之间的证据链' },
  { id: 'propagation', label: '传播关系', description: '账号发布、引用及内容主题关系' },
  { id: 'evidence', label: '数据血缘', description: '批次、分类、源文件与质量问题' },
]

const NODE_COLORS = {
  dataset: '#193f78', person: '#1f6a62', account: '#2673a8', event: '#b3534d', content: '#7b5aa6',
  topic: '#a87524', quoted_account: '#4d7994', source: '#64748b', source_file: '#64748b',
  category: '#346a92', quality: '#b85f32',
}

function useHashRoute() {
  const parse = () => {
    const raw = window.location.hash.replace(/^#\/?/, '') || 'overview'
    const [path, query = ''] = raw.split('?')
    const route = ROUTE_META[path] ? path : 'overview'
    if (route !== path && window.location.hash) window.history.replaceState(null, '', '#/overview')
    return { route, params: new URLSearchParams(route === path ? query : '') }
  }
  const [state, setState] = useState(parse)
  useEffect(() => {
    const onHashChange = () => setState(parse())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  const navigate = (route, params = {}) => {
    const query = new URLSearchParams(params).toString()
    window.location.hash = `/${route}${query ? `?${query}` : ''}`
  }
  return { ...state, navigate }
}

function useLoad(loader, deps) {
  const [state, setState] = useState({ data: null, loading: true, error: '' })
  const [version, setVersion] = useState(0)
  useEffect(() => {
    let active = true
    setState((current) => ({ ...current, loading: true, error: '' }))
    Promise.resolve().then(loader)
      .then((data) => active && setState({ data, loading: false, error: '' }))
      .catch((error) => active && setState({ data: null, loading: false, error: error.message || '加载失败' }))
    return () => { active = false }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version])
  return { ...state, reload: () => setVersion((value) => value + 1) }
}

function useEscapeClose(onClose) {
  useEffect(() => {
    const closeOnEscape = (event) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])
}

function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '未提供'
  return new Intl.NumberFormat('zh-CN').format(Number(value))
}

function formatDateTime(value) {
  if (!value) return '未记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

function shorten(value, max = 34) {
  const text = String(value || '')
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function statusLabel(status) {
  return ({
    TEST_READY: '测试就绪', COMPLETED: '已完成', VALIDATED: '已验证', IMPLEMENTED: '已实现',
    SCAFFOLDED: '已搭建', PARTIAL: '部分实现', NOT_CONFIGURED: '未配置', NOT_RUN: '未运行',
    SAMPLE_READY: '样本可用', PUBLIC_READY: '公开样本可用', AUTH_REQUIRED: '需官方授权', PUBLIC_SAMPLE: '公开样本',
    DRAFT: '草稿', PAUSED: '已暂停', ARCHIVED: '已归档', PENDING: '待处理',
    ACKNOWLEDGED: '已确认', RESOLVED: '已完成', PENDING_VERIFICATION: '待核验', OPEN: '待处理', SUCCESS: '成功',
  })[status] || status || '未标注'
}

function statusTone(status) {
  if (['TEST_READY', 'COMPLETED', 'VALIDATED', 'IMPLEMENTED', 'SUCCESS', 'ACKNOWLEDGED', 'RESOLVED'].includes(status)) return 'success'
  if (['OPEN', 'PENDING', 'PENDING_VERIFICATION', 'PARTIAL', 'PAUSED'].includes(status)) return 'warning'
  if (['HIGH', 'ERROR', 'FAILED'].includes(status)) return 'danger'
  return 'neutral'
}

function evidenceLabel(value) { return EVIDENCE_LABELS[value] || value || '未标注' }

function App() {
  const { route, params, navigate } = useHashRoute()
  const [role, setRole] = useState(() => localStorage.getItem('monitor-role') || 'core')
  const [mobileOpen, setMobileOpen] = useState(false)
  const health = useLoad(() => api.get('/api/health'), [])
  useEffect(() => { localStorage.setItem('monitor-role', role) }, [role])
  useEffect(() => { setMobileOpen(false); window.scrollTo({ top: 0 }) }, [route])
  const pages = {
    overview: <OverviewPage role={role} navigate={navigate} />,
    targets: <TargetsPage role={role} />,
    collection: <CollectionPage role={role} />,
    topics: <TopicsPage role={role} params={params} navigate={navigate} />,
    knowledge: <KnowledgePage role={role} initialQuery={params.get('q') || ''} />,
    graph: <GraphPage role={role} />,
    analysis: <AnalysisPage role={role} />,
    alerts: <AlertsPage role={role} />,
    objects: <ObjectsPage role={role} />,
    opinion: <OpinionPage role={role} />,
    reports: <ReportsPage role={role} />,
    chat: <ChatPage role={role} />,
  }
  return (
    <div className="app-shell">
      <Sidebar route={route} navigate={navigate} open={mobileOpen} onClose={() => setMobileOpen(false)} />
      {mobileOpen && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setMobileOpen(false)} />}
      <div className="workspace">
        <Topbar route={route} role={role} setRole={setRole} navigate={navigate} onMenu={() => setMobileOpen(true)} health={health} />
        <main className="main-content">{pages[route]}</main>
      </div>
    </div>
  )
}

function Sidebar({ route, navigate, open, onClose }) {
  return (
    <aside className={`sidebar ${open ? 'is-open' : ''}`}>
      <div className="brand">
        <div className="brand-mark"><Target size={20} strokeWidth={2.2} /></div>
        <div className="brand-copy"><strong>海外舆情监测</strong><span>研究工作台</span></div>
        <button className="icon-btn sidebar-close" aria-label="关闭导航" onClick={onClose}><X size={18} /></button>
      </div>
      <nav className="side-nav" aria-label="主导航">
        {NAV_GROUPS.map((group) => (
          <div className="nav-group" key={group.label}>
            <div className="nav-group-label">{group.label}</div>
            {group.items.map((item) => {
              const Icon = item.icon
              return <button key={item.id} className={`nav-link ${route === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)}><Icon size={17} /><span>{item.label}</span>{route === item.id && <ChevronRight size={15} className="nav-chevron" />}</button>
            })}
          </div>
        ))}
      </nav>
      <div className="sidebar-footer">
        <div className="local-mode"><HardDrive size={17} /><div><strong>本地化运行</strong><span>数据不出域 · 操作留痕</span></div></div>
        <div className="version-line">产品验证版 V1.0</div>
      </div>
    </aside>
  )
}

function Topbar({ route, role, setRole, navigate, onMenu, health }) {
  const [query, setQuery] = useState('')
  const submit = (event) => { event.preventDefault(); if (query.trim()) navigate('knowledge', { q: query.trim() }) }
  return (
    <header className="topbar">
      <div className="topbar-left"><button className="icon-btn menu-btn" aria-label="打开导航" onClick={onMenu}><Menu size={20} /></button><div className="route-title"><span>海外舆情监测系统</span><strong>{ROUTE_META[route]?.label}</strong></div></div>
      <form className="global-search" onSubmit={submit}><Search size={17} /><input aria-label="全局知识检索" maxLength="500" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索当前知识库…" /><button type="submit" aria-label="开始检索"><ArrowRight size={16} /></button></form>
      <div className="topbar-actions">
        <div className={`service-state ${health.error ? 'offline' : health.loading ? 'checking' : 'online'}`} title={health.error || '后端服务状态'}><span /><span className="service-copy">{health.error ? '服务未连接' : health.loading ? '检查中' : '本地服务可用'}</span></div>
        <label className="role-select"><UserRound size={16} /><select value={role} onChange={(event) => setRole(event.target.value)} aria-label="切换数据权限视角"><option value="core">核心课题组</option><option value="researcher">研究员</option></select><ChevronDown size={14} /></label>
      </div>
    </header>
  )
}

function PageHeader({ eyebrow, title, description, actions }) {
  return <div className="page-header"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1>{description && <p>{description}</p>}</div>{actions && <div className="page-actions">{actions}</div>}</div>
}

function Button({ children, icon: Icon, variant = 'primary', disabled, onClick, type = 'button', title, className = '' }) {
  return <button className={`button ${variant} ${className}`} type={type} onClick={onClick} disabled={disabled} title={title}>{Icon && <Icon size={16} />}{children}</button>
}
function Badge({ children, tone = 'neutral' }) { return <span className={`badge ${tone}`}>{children}</span> }
function Notice({ children, tone = 'info', icon: Icon = Info }) { return <div className={`notice ${tone}`}><Icon size={18} /><div>{children}</div></div> }
function Panel({ title, subtitle, action, children, className = '' }) { return <section className={`panel ${className}`}>{(title || action) && <div className="panel-heading"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>{action}</div>}{children}</section> }
function LoadingPanel({ label = '正在读取本地数据…' }) { return <div className="state-panel"><RefreshCw size={22} className="spin" /><strong>{label}</strong></div> }
function ErrorPanel({ message, onRetry }) { return <div className="state-panel error-state"><CircleAlert size={24} /><strong>数据服务暂不可用</strong><span>{message}</span>{onRetry && <Button variant="secondary" icon={RefreshCw} onClick={onRetry}>重新加载</Button>}</div> }
function Metric({ label, value, helper, icon: Icon, tone = 'blue' }) { return <div className="metric-card"><div className={`metric-icon ${tone}`}><Icon size={20} /></div><div><span>{label}</span><strong>{value}</strong><small>{helper}</small></div></div> }

function OverviewPage({ role, navigate }) {
  const state = useLoad(() => Promise.all([api.get(`/api/overview?role=${role}`), api.get(`/api/collection?role=${role}`)]).then(([overview, collection]) => ({ overview, collection })), [role])
  const publicTopics = useLoad(() => api.get(`/api/public-demo/topics?role=${role}`), [role])
  if (state.loading && !state.data) return <LoadingPanel />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const { overview, collection } = state.data
  const metrics = overview.metrics || {}; const batch = overview.batch || {}
  const isPublic = overview.mode === 'PUBLIC_WEB_SAMPLE' || batch.code === 'PUBLIC-WEB-20260827'
  const categoryEntries = Object.entries(batch.category_counts || {})
  const maxEvidence = Math.max(...(overview.evidence_distribution || []).map((item) => item.count), 1)
  return (
    <div className="page-stack">
      <PageHeader eyebrow="运行总览 / OVERVIEW" title="系统概览" description={isPublic ? '以公开网页试采批次展示采集、治理、检索、专题与证据下钻流程。' : '以本地测试批次验证采集、治理、检索、图谱、研判与输出的完整流程。'} actions={<Button variant="secondary" icon={RefreshCw} onClick={state.reload}>刷新</Button>} />
      <Notice tone={isPublic ? 'info' : 'test'} icon={isPublic ? Globe2 : TestTube2}><strong>{isPublic ? '当前为公开网页试采模式。' : '当前为测试数据模式。'}</strong> {overview.notice}</Notice>
      <div className="metric-grid">
        <Metric label="结构化记录" value={formatNumber(metrics.records)} helper={`${categoryEntries.length} 个数据类别`} icon={Database} tone="blue" />
        <Metric label="知识索引" value={formatNumber(metrics.knowledge_chunks)} helper="本地持久化知识块" icon={Library} tone="teal" />
        <Metric label="事件线索" value={formatNumber(metrics.events)} helper="来自源文件结构化结果" icon={BellRing} tone="amber" />
        <Metric label="开放质量问题" value={formatNumber(metrics.quality_open)} helper="冲突与生产字段缺口" icon={CircleAlert} tone="red" />
      </div>
      <Panel title="重点专题" subtitle="公开网页试采样本，支持专题与原文证据下钻" action={<Button variant="ghost" icon={ArrowRight} onClick={() => navigate('topics')}>查看全部专题</Button>}>
        {publicTopics.loading && !publicTopics.data ? <div className="topic-home-loading">正在读取专题…</div> : publicTopics.error ? <div className="topic-home-loading muted">专题服务暂不可用</div> : <div className="topic-home-grid">{(publicTopics.data?.items || []).slice(0, 3).map((item) => <button className="topic-home-card" key={item.slug || item.name} onClick={() => navigate('topics', { topic: item.slug })}><span>{publicTopicName(item)}</span><strong>{formatNumber(item.count)} 条</strong><ChevronRight size={15} /></button>)}{!(publicTopics.data?.items || []).length && <span className="muted">暂无公开网页样本。</span>}</div>}
      </Panel>
      <div className="two-column wide-left">
        <Panel title="当前数据批次" subtitle="可被后续正式采集批次替换，不绑定特定专题" action={<Button variant="ghost" icon={ArrowRight} onClick={() => navigate('collection')}>查看批次</Button>}>
          <div className="batch-hero"><div className="batch-icon"><FileArchive size={25} /></div><div className="batch-main"><div className="batch-title-row"><strong>{batch.name}</strong><Badge tone="success">{statusLabel(batch.status)}</Badge></div><p>{batch.purpose}</p><div className="meta-line"><span>批次号 {batch.code}</span><span>源数据截至 {batch.source_date}</span><span>{batch.source_files?.length || 0} 份源文件</span></div></div></div>
          <div className="category-strip">{categoryEntries.map(([key, count]) => <div key={key}><span>{CATEGORY_LABELS[key] || key}</span><strong>{count}</strong></div>)}</div>
        </Panel>
        <Panel title="流程状态" subtitle="只展示本批次已实际执行的处理环节"><div className="pipeline-list compact">{(collection.pipeline || []).map((step, index) => <div className="pipeline-step" key={step.id}><div className="pipeline-index">{index + 1}</div><div><strong>{step.name}</strong><span>{formatNumber(step.value)} {step.unit}</span></div><CheckCircle2 size={17} /></div>)}</div></Panel>
      </div>
      <div className="two-column">
        <Panel title="证据性质分布" subtitle="区分直接证据、来源转述、研究分析与数据缺口"><div className="bar-list">{(overview.evidence_distribution || []).map((item) => <div className="bar-row" key={item.evidence_type}><div><span>{evidenceLabel(item.evidence_type)}</span><strong>{item.count}</strong></div><div className="bar-track"><i style={{ width: `${(item.count / maxEvidence) * 100}%` }} /></div></div>)}</div></Panel>
        <Panel title="接入准备度" subtitle="平台适配器注册不等于实时采集已接通"><div className="readiness-block"><div className="readiness-score"><strong>{metrics.connectors_ready || 0}</strong><span>/ {metrics.connectors_total || 0} 已就绪</span></div><div className="progress-track"><i style={{ width: `${metrics.connectors_total ? (metrics.connectors_ready / metrics.connectors_total) * 100 : 0}%` }} /></div><p>{collection.notice}</p><Button variant="secondary" icon={Server} onClick={() => navigate('collection')}>查看连接器</Button></div></Panel>
      </div>
    </div>
  )
}

function TargetsPage({ role }) {
  const state = useLoad(() => api.get(`/api/targets?role=${role}`), [role])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null)
  if (state.loading && !state.data) return <LoadingPanel />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const items = state.data?.items || []
  const filtered = items.filter((item) => `${item.name} ${item.name_en} ${item.role} ${item.handle}`.toLowerCase().includes(query.toLowerCase()))
  return (
    <div className="page-stack">
      <PageHeader eyebrow="监测与数据 / TARGETS" title="监测对象" description="从当前数据批次读取人物与关联账号；正式对象库可在后续采集阶段替换或扩充。" />
      {role === 'researcher' && <Notice tone="warning" icon={LockKeyhole}>当前研究员视角隐藏了 {state.data.hidden_restricted || 0} 条受限人物记录。</Notice>}
      <div className="toolbar"><label className="search-input"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索姓名、身份或账号" /></label><div className="toolbar-count">显示 {filtered.length} / {items.length} 个对象</div></div>
      <Panel className="table-panel">
        <div className="table-scroll">
          <table className="data-table"><thead><tr><th>对象</th><th>关系 / 身份</th><th>账号</th><th>主题</th><th>证据性质</th><th>数据日期</th><th /></tr></thead><tbody>
            {filtered.map((item) => <tr key={item.id} className="clickable" tabIndex="0" onClick={() => setSelected(item)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelected(item) } }}><td><div className="identity-cell"><span className="initial-avatar">{(item.name || '?').slice(0, 1)}</span><div><strong>{item.name}</strong><small>{item.name_en || '英文名未提供'}</small></div></div></td><td><strong>{item.relation || '未提供'}</strong><small className="cell-subtext">{item.role || '身份未提供'}</small></td><td>{item.handle || '未提供'}<small className="cell-subtext">关注量 {item.followers}</small></td><td><div className="tag-list">{(item.themes || []).slice(0, 2).map((tag) => <span key={tag}>{tag}</span>)}{!(item.themes || []).length && <span className="muted">未提供</span>}</div></td><td><Badge>{evidenceLabel(item.evidence)}</Badge></td><td>{item.source_date}</td><td><ChevronRight size={16} /></td></tr>)}
          </tbody></table>
          {!filtered.length && <EmptyState icon={Search} title="没有匹配的监测对象" description="请调整搜索关键词。" />}
        </div>
      </Panel>
      {selected && <TargetDrawer item={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}

function TargetDrawer({ item, onClose }) {
  useEscapeClose(onClose)
  return <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer" role="dialog" aria-modal="true" aria-label={`${item.name}对象详情`}><div className="drawer-header"><div><span>对象详情</span><h2>{item.name}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭对象详情"><X size={19} /></button></div><div className="drawer-body"><div className="profile-block"><span className="initial-avatar large">{(item.name || '?').slice(0, 1)}</span><div><strong>{item.name_en || '英文名未提供'}</strong><span>{item.role || item.relation || '身份未提供'}</span></div></div><DefinitionList items={[["关系", item.relation || '未提供'], ['关联账号', item.handle || '未提供'], ['源文件关注量', item.followers], ['敏感级别', item.sensitivity], ['证据性质', evidenceLabel(item.evidence)], ['数据日期', item.source_date]]} /><div className="detail-section"><h3>源文件主题标签</h3><div className="tag-list roomy">{(item.themes || []).map((tag) => <span key={tag}>{tag}</span>)}{!(item.themes || []).length && <span className="muted">未提供主题标签</span>}</div></div><Notice tone="info">该对象详情仅复现当前测试批次字段，未自动补全缺失账号、实时关注量或外部履历。</Notice></div></aside></div>
}

const PUBLIC_TOPIC_LABELS = {
  xiongan: '雄安新区',
  'xiongan-new-area': '雄安新区',
  apec: 'APEC 2026',
  'apec-2026': 'APEC 2026',
  'xi-overseas': '习近平海外活动',
  xi: '习近平海外活动',
}

function publicTopicSlug(item) {
  const explicit = item?.slug || item?.topic_slug || item?.code || item?.id
  if (explicit) return explicit
  const label = String(item?.name || item?.title || item?.topic || '').toLowerCase()
  if (label.includes('雄安') || label.includes('xiongan')) return 'xiongan'
  if (label.includes('apec')) return 'apec-2026'
  if (label.includes('习近平') || label.includes('xi ')) return 'xi-overseas'
  return ''
}

function publicTopicName(item) {
  return item?.name || item?.title || item?.topic || PUBLIC_TOPIC_LABELS[publicTopicSlug(item)] || publicTopicSlug(item) || '未命名专题'
}

function publicTopicItems(payload) {
  return payload?.items || payload?.records || payload?.results || payload?.topic?.items || []
}

function publicValues(value) {
  if (Array.isArray(value)) return value.filter((item) => item !== null && item !== undefined && item !== '')
  if (value && typeof value === 'object') return Object.values(value).filter((item) => item !== null && item !== undefined && item !== '')
  return value ? [value] : []
}

function publicFacetNames(value) {
  return publicValues(value).map((item) => {
    if (item && typeof item === 'object') {
      const name = item.name || item.label || item.value || '未标注'
      return item.count !== undefined ? `${name}（${item.count}）` : name
    }
    return String(item)
  })
}

function TopicsPage({ role, params, navigate }) {
  const selectedSlug = params.get('topic') || ''
  const listState = useLoad(() => api.get(`/api/public-demo/topics?role=${role}`), [role])
  const detailState = useLoad(() => selectedSlug
    ? api.get(`/api/public-demo/topics/${encodeURIComponent(selectedSlug)}?role=${role}`)
    : Promise.resolve(null), [role, selectedSlug])
  const [query, setQuery] = useState('')
  const [selectedRecord, setSelectedRecord] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [actionError, setActionError] = useState('')

  useEffect(() => { setSelectedRecord(null); setActionError('') }, [selectedSlug])

  const refresh = async () => {
    if (role !== 'core' || refreshing) return
    setRefreshing(true); setActionError('')
    try {
      await api.post('/api/public-demo/refresh', { role })
      listState.reload(); detailState.reload()
    } catch (error) { setActionError(error.message || '公开样本刷新失败') }
    finally { setRefreshing(false) }
  }

  if (listState.loading && !listState.data) return <LoadingPanel label="正在读取重点专题…" />
  if (listState.error) return <ErrorPanel message={listState.error} onRetry={listState.reload} />

  const payload = listState.data || {}
  const topics = (payload.topics || payload.items || []).filter((item) => {
    const text = `${publicTopicName(item)} ${item.description || item.summary || ''} ${publicFacetNames(item.platforms).join(' ')}`.toLowerCase()
    return !query.trim() || text.includes(query.trim().toLowerCase())
  })
  const status = payload.status || payload.summary || payload

  if (selectedSlug) {
    const detailPayload = detailState.data || {}
    const selectedSummary = topics.find((item) => publicTopicSlug(item) === selectedSlug)
      || (payload.topics || payload.items || []).find((item) => publicTopicSlug(item) === selectedSlug)
      || {}
    const detailTopic = detailPayload.topic || detailPayload.summary || detailPayload
    const items = publicTopicItems(detailPayload).length ? publicTopicItems(detailPayload) : (selectedSummary.items_preview || selectedSummary.preview || [])
    const topicName = publicTopicName(detailTopic && Object.keys(detailTopic).length ? detailTopic : selectedSummary)
    const platforms = publicFacetNames(detailTopic.platforms || selectedSummary.platforms)
    const countries = publicFacetNames(detailTopic.countries || detailTopic.country_regions || selectedSummary.countries || selectedSummary.country_regions)
    const keywords = publicValues(detailTopic.keywords || selectedSummary.keywords || [topicName])
    return (
      <div className="page-stack">
        <PageHeader eyebrow="监测与数据 / TOPIC DETAIL" title={topicName} description="按专题聚合公开网页试采记录；每条记录都保留原文地址、采集时间和证据性质。" actions={<><Button variant="secondary" icon={ArrowLeft} onClick={() => navigate('topics')}>返回专题</Button><Button icon={RefreshCw} disabled={role !== 'core' || refreshing} title={role !== 'core' ? '研究员视角为只读' : ''} onClick={refresh}>{refreshing ? '刷新中…' : '刷新样本'}</Button></>} />
        {actionError && <Notice tone="danger" icon={CircleAlert}>{actionError}</Notice>}
        {detailState.loading && !detailState.data ? <LoadingPanel label="正在读取专题记录…" /> : detailState.error ? <ErrorPanel message={detailState.error} onRetry={detailState.reload} /> : <>
          <Notice tone="info" icon={Globe2}><strong>公开网页试采。</strong> 当前记录来自可直接访问的公开页面或公开 RSS 元数据；需要登录、验证码或官方授权的平台不会被绕过。</Notice>
          <div className="topic-metrics">
            <div><div className="metric-icon blue"><Database size={18} /></div><span>记录数</span><strong>{formatNumber(detailTopic.count ?? detailTopic.record_count ?? items.length)}</strong></div>
            <div><div className="metric-icon teal"><Globe2 size={18} /></div><span>来源平台</span><strong>{formatNumber(platforms.length)}</strong></div>
            <div><div className="metric-icon amber"><MapPin size={18} /></div><span>国家 / 地区</span><strong>{formatNumber(countries.length)}</strong></div>
            <div><div className="metric-icon red"><CalendarClock size={18} /></div><span>最近采集</span><strong>{shorten(formatDateTime(detailTopic.collected_at || detailTopic.latest_collected_at || status.collected_at), 22)}</strong></div>
          </div>
          <div className="two-column wide-left">
            <Panel title="专题研判摘要" subtitle="仅展示公开样本中的可验证字段，不代替人工研判">
              <p className="lead-copy">{detailTopic.description || detailTopic.summary || detailTopic.analysis?.narrative || selectedSummary.description || selectedSummary.summary || '当前专题已形成公开网页样本，可从下方记录继续下钻。'}</p>
              <div className="topic-detail-block"><h3>关键词</h3><div className="tag-list roomy">{keywords.map((item) => <span key={item}>{item}</span>)}</div></div>
              <DefinitionList items={[["情感分析", '未运行'], ['立场识别', '未运行'], ['风险研判', '待人工核验'], ['采集范围', detailTopic.scope || selectedSummary.scope || '公开网页与公开 RSS'], ['样本标识', detailTopic.demo_label || selectedSummary.demo_label || '公开网页试采']]} />
            </Panel>
            <Panel title="来源分布" subtitle="平台、国家和语言字段来自采集记录"><div className="topic-facet-list"><div><span><Globe2 size={14} />平台</span><strong>{platforms.length ? platforms.join(' · ') : '未提供'}</strong></div><div><span><MapPin size={14} />国家 / 地区</span><strong>{countries.length ? countries.join(' · ') : '未提供'}</strong></div><div><span><Languages size={14} />语言</span><strong>{publicFacetNames(detailTopic.languages || selectedSummary.languages).join(' · ') || '中文 / 英文混合'}</strong></div></div></Panel>
          </div>
          <Panel title="专题记录" subtitle="点击记录查看原文摘要、结构化字段和来源锚点" action={<Badge>{items.length} 条</Badge>}>
            <div className="topic-record-list">{items.map((item, index) => <PublicTopicRecord key={item.record_id || item.id || item.original_url || index} item={item} index={index} role={role} onSelect={setSelectedRecord} />)}{!items.length && <EmptyState icon={FileSearch} title="当前专题没有可展示记录" description="请刷新公开样本或返回专题列表。" />}</div>
          </Panel>
        </>}
        {selectedRecord && <PublicRecordDrawer item={selectedRecord} onClose={() => setSelectedRecord(null)} />}
      </div>
    )
  }

  return (
    <div className="page-stack">
      <PageHeader eyebrow="监测与数据 / TOPICS" title="重点专题" description="围绕演示验收场景聚合公开网页样本，支持专题、平台、国家与原文证据逐级下钻。" actions={<Button icon={RefreshCw} disabled={role !== 'core' || refreshing} title={role !== 'core' ? '研究员视角为只读' : ''} onClick={refresh}>{refreshing ? '刷新中…' : '刷新公开样本'}</Button>} />
      {actionError && <Notice tone="danger" icon={CircleAlert}>{actionError}</Notice>}
      <Notice tone="info" icon={Globe2}><strong>公开网页试采样本。</strong> 已将可直接访问的公开页面和 RSS 元数据整理为可检索批次；社交平台登录墙、验证码和官方 API 授权不会被绕过。</Notice>
      <div className="topic-status-line"><Badge tone="success">{status.demo_label || '公开网页试采'}</Badge><span>采集时间 {formatDateTime(status.collected_at || status.latest_collected_at)}</span><span>{formatNumber(status.record_count ?? status.records ?? topics.reduce((sum, item) => sum + Number(item.count || item.record_count || 0), 0))} 条记录</span><span>{formatNumber(status.channel_count ?? 12)} 个巡检渠道</span><span>{formatNumber(status.platform_count ?? status.platforms?.length ?? 0)} 个入库来源</span></div>
      <div className="toolbar topic-toolbar"><label className="search-input"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength="120" placeholder="搜索专题、平台或关键词" /></label><div className="toolbar-count">显示 {topics.length} / {(payload.topics || payload.items || []).length} 个专题</div></div>
      <div className="topic-grid">{topics.map((item) => <TopicCard key={publicTopicSlug(item) || publicTopicName(item)} item={item} onOpen={() => navigate('topics', { topic: publicTopicSlug(item) })} />)}{!topics.length && <Panel><EmptyState icon={Search} title="没有匹配专题" description="请调整搜索关键词。" /></Panel>}</div>
      <Panel title="渠道访问观察" subtitle="样本状态不等于实时连接器已授权"><div className="topic-observation-list">{(payload.platform_access_observations || status.platform_access_observations || []).map((item) => <div key={item.platform || item.name}><span>{item.platform || item.name}</span><Badge tone={String(item.access_status || item.status || '').startsWith('PUBLIC') ? 'success' : 'warning'}>{item.access_status || item.status || '待观察'}</Badge><p>{item.reason || item.observation || item.note || item.limitation || item.status || '未提供观察说明'}</p></div>)}</div>{!(payload.platform_access_observations || status.platform_access_observations || []).length && <p className="muted">暂无渠道观察信息。</p>}</Panel>
    </div>
  )
}

function TopicCard({ item, onOpen }) {
  const platforms = publicFacetNames(item.platforms)
  const countries = publicFacetNames(item.countries || item.country_regions)
  return <button className="topic-card" onClick={onOpen}><div className="topic-card-head"><div className="topic-card-icon"><Target size={20} /></div><div><Badge tone="success">公开样本</Badge><h2>{publicTopicName(item)}</h2></div><ChevronRight size={18} /></div><p>{item.description || item.summary || '公开网页试采专题，点击查看记录和来源证据。'}</p><div className="topic-card-meta"><span><Database size={13} />{formatNumber(item.count ?? item.record_count ?? item.total)} 条记录</span><span><Globe2 size={13} />{platforms.length ? platforms.join(' · ') : '平台未提供'}</span><span><MapPin size={13} />{countries.length ? countries.join(' · ') : '地区未提供'}</span></div><div className="topic-card-foot"><span>最近更新 {formatDateTime(item.latest_collected_at || item.collected_at || item.updated_at)}</span><span>查看证据 <ArrowRight size={13} /></span></div></button>
}

function PublicTopicRecord({ item, index, role, onSelect }) {
  const title = item.title || item.name || item.original_title || '无标题记录'
  const url = item.original_url || item.source_url || item.url || ''
  const platform = item.platform || item.source || item.channel || '公开网页'
  const published = item.published_at || item.published_time || item.publish_time
  return <article className="topic-record"><button className="topic-record-main" onClick={async () => {
    const recordId = item.record_id || item.id
    if (!recordId) { onSelect(item); return }
    try { onSelect(await api.get(`/api/records/${encodeURIComponent(recordId)}?role=${role}`)) } catch { onSelect(item) }
  }}><span className="topic-record-index">{String(index + 1).padStart(2, '0')}</span><span className="topic-record-copy"><strong>{title}</strong><small>{item.summary || item.description || '没有可展示摘要。'}</small><span className="topic-record-meta"><span>{platform}</span>{published && <span>{formatDateTime(published)}</span>}{item.language && <span>{item.language}</span>}</span></span><ChevronRight size={16} /></button>{url && <a className="topic-record-link" href={url} target="_blank" rel="noreferrer" aria-label={`打开 ${title} 原文`} title="打开原文"><ExternalLink size={15} /></a>}</article>
}

function PublicRecordDrawer({ item, onClose }) {
  useEscapeClose(onClose)
  const content = item.content || item.raw || item
  const value = (...keys) => keys.map((key) => content?.[key] ?? item?.[key]).find((entry) => entry !== undefined && entry !== null && entry !== '')
  const title = item.title || content.title || content.original_title || '记录详情'
  const url = value('original_url', 'source_url', 'url')
  const interaction = value('interaction', 'engagement', 'metrics')
  const sourceRefs = publicValues(item.source_refs || (url ? [url] : []))
  const fields = [
    ['平台', value('platform', 'source', 'channel')], ['作者 / 频道', value('author_or_channel', 'author_name', 'author')],
    ['国家 / 地区', value('country_region', 'country', 'region')], ['语言', value('language')],
    ['发布时间', value('published_at', 'published_time', 'publish_time')], ['采集时间', value('collected_at', 'collection_time', 'created_at')],
    ['关键词 / 专题', value('topic', 'keywords')], ['情感', value('sentiment') || '未运行'], ['立场', value('stance') || '未运行'], ['风险', value('risk', 'risk_grade') || '待人工研判'],
  ]
  const translation = value('translation_zh', 'translated_text')
  return <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer wide" role="dialog" aria-modal="true" aria-label="公开记录详情"><div className="drawer-header"><div><span>{value('platform', 'source', 'channel') || '公开网页试采'} · {evidenceLabel(item.evidence_type || content.evidence_type || 'source_snapshot')}</span><h2>{title}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭记录详情"><X size={19} /></button></div><div className="drawer-body"><div className="tag-list roomy"><Badge tone="success">公开样本</Badge><Badge>{evidenceLabel(item.evidence_type || content.evidence_type || 'source_snapshot')}</Badge>{(item.sensitivity || content.sensitivity) && <Badge>{item.sensitivity || content.sensitivity}</Badge>}</div><p className="lead-copy">{item.summary || content.summary || content.description || '没有可展示摘要。'}</p><div className="detail-section"><h3>原文与采集字段</h3><DefinitionList items={fields.map(([label, fieldValue]) => [label, Array.isArray(fieldValue) ? fieldValue.join(' · ') : (fieldValue || '未提供')])} /></div><div className="detail-section"><h3>中文翻译</h3><p className="raw-evidence">{translation || `当前未配置机器翻译（${value('translation_status') || 'NOT_CONFIGURED'}）。`}</p></div>{interaction && <div className="detail-section"><h3>互动指标</h3><div className="json-fields"><div><span>互动数据</span><strong>{typeof interaction === 'object' ? JSON.stringify(interaction, null, 2) : String(interaction)}</strong></div></div></div>}<div className="detail-section"><h3>原文摘要</h3><p className="raw-evidence">{value('original_text', 'text', 'content_text') || item.summary || content.summary || '当前批次只保存公开页面摘要，没有复制受限正文。'}</p></div><div className="detail-section"><h3>来源锚点</h3>{sourceRefs.length ? <div className="file-list">{sourceRefs.map((source) => /^https?:\/\//i.test(String(source)) ? <a className="source-link" href={source} target="_blank" rel="noreferrer" key={source}><ExternalLink size={14} />{shorten(source, 68)}</a> : <span key={source}><FileCheck2 size={14} />{source}</span>)}</div> : <p className="muted">未提供可点击的原始 URL。</p>}</div></div></aside></div>
}

function CollectionPage({ role }) {
  const state = useLoad(() => Promise.all([api.get(`/api/collection?role=${role}`), api.get(`/api/datasets?role=${role}`)]).then(([collection, datasets]) => ({ collection, datasets })), [role])
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [selectedRecord, setSelectedRecord] = useState(null)
  const [showTaskForm, setShowTaskForm] = useState(false)
  const [taskMessage, setTaskMessage] = useState('')
  if (state.loading && !state.data) return <LoadingPanel />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const { collection, datasets } = state.data
  const batch = datasets.items?.[0] || collection.batch || {}
  const isPublic = batch.code === 'PUBLIC-WEB-20260827' || collection.public_demo?.batch?.code === 'PUBLIC-WEB-20260827'
  return (
    <div className="page-stack">
      <PageHeader eyebrow="监测与数据 / COLLECTION" title="采集与数据批次" description={isPublic ? '管理公开网页试采批次、连接器准备状态、任务草稿与结构化记录；样本可按相同数据契约替换。' : '管理连接器准备状态、采集任务草稿、批次处理流程与结构化记录；测试样本不是固定专题。'} actions={<><Button icon={Database} disabled={role !== 'core'} title={role !== 'core' ? '研究员视角为只读' : ''} onClick={() => setShowTaskForm((value) => !value)}>{showTaskForm ? '收起任务表单' : '新建采集任务'}</Button><Button variant="secondary" icon={UploadCloud} disabled title="正式导入入口尚未开放">导入新批次</Button></>} />
      <Notice tone={isPublic ? 'info' : 'test'} icon={isPublic ? Globe2 : TestTube2}><strong>{isPublic ? '公开网页试采批次。' : '替换式测试批次。'}</strong> {isPublic ? '当前批次保存公开页面标题、短摘要、发布时间、采集时间和原文链接；正式连接器仍按授权状态展示。' : '当前 ZIP 仅用于跑通产品流程；接入正式来源后可按相同数据契约替换。'}</Notice>
      {taskMessage && <Notice tone="info" icon={CheckCircle2}>{taskMessage}</Notice>}
      <Panel title="平台连接状态" subtitle="采集任务使用的适配器状态；正式授权完成前不会执行采集"><div className="connector-grid">{(collection.connectors || []).map((connector) => <div className="connector-card" key={connector.id}><div className="connector-card-head"><div className="connector-icon">{connector.channel_type === 'social' ? <MessageSquareText size={16} /> : <FileSearch size={16} />}</div><div><strong>{connector.name}</strong><span>{connector.channel_type === 'social' ? '社交平台' : '媒体来源'}</span></div><Badge>{statusLabel(connector.status)}</Badge></div><div className="connector-card-meta"><span>{connector.supported_media}</span><button disabled title="需要正式数据源授权" aria-label={`配置 ${connector.name}`}><PanelLeftClose size={15} /></button></div></div>)}</div></Panel>
      {showTaskForm && role === 'core' && <TaskDraftForm connectors={collection.connectors || []} role={role} onCancel={() => setShowTaskForm(false)} onSaved={(payload) => { setTaskMessage(payload.execution_notice || '采集任务草稿已保存。'); setShowTaskForm(false); state.reload() }} />}
      <Panel title="批次处理链路" subtitle="本批次各环节的实际处理结果"><div className="pipeline-grid">{(collection.pipeline || []).map((step, index) => <div className={`pipeline-card ${step.status === 'INCOMPLETE' ? 'incomplete' : ''}`} key={step.id}><div className="pipeline-card-top"><span>{String(index + 1).padStart(2, '0')}</span>{step.status === 'INCOMPLETE' ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}</div><strong>{step.name}</strong><p>{formatNumber(step.value)} {step.unit}</p></div>)}</div></Panel>
      <div className="two-column wide-left">
        <Panel title="数据批次" subtitle="批次元数据、来源文件和分类计数"><div className="dataset-card"><div className="dataset-card-head"><div className="batch-icon"><FileArchive size={23} /></div><div><strong>{batch.name}</strong><span>{batch.code}</span></div><Badge tone="success">{statusLabel(batch.status)}</Badge></div><p>{batch.purpose}</p><DefinitionList items={[["源数据日期", batch.source_date], ['记录总量', formatNumber(batch.record_count)], ['源文件数量', batch.source_files?.length || 0], ['质量问题', batch.quality_issue_count ?? collection.batch?.quality_issue_count ?? '未统计'], ['校验哈希', shorten(batch.checksum, 18)]]} /><div className="detail-section"><h3>来源文件</h3><div className="file-list">{(batch.source_files || []).map((file) => <span key={file}><FileText size={14} />{file}</span>)}</div></div></div></Panel>
      </div>
      <Panel title="采集任务" subtitle="任务可配置并保存为草稿；连接器完成授权前不会执行采集" action={<Badge>{(collection.tasks || []).length} 个任务</Badge>}><TaskList tasks={collection.tasks || []} connectors={collection.connectors || []} canCreate={role === 'core'} onNew={() => setShowTaskForm(true)} /></Panel>
      <Panel title="结构化记录浏览器" subtitle="按分类和关键词查看本批次记录，保留证据性质与来源锚点">
        <form className="record-toolbar" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(query.trim()) }}><label className="search-input"><Search size={17} /><input maxLength="300" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索记录标题或摘要" /></label><select value={category} onChange={(event) => setCategory(event.target.value)} aria-label="筛选数据类别"><option value="">全部类别</option>{Object.entries(batch.category_counts || {}).map(([key, count]) => <option value={key} key={key}>{CATEGORY_LABELS[key] || key}（{count}）</option>)}</select><Button type="submit" icon={Search}>查询</Button></form>
        <DatasetRecords batchCode={batch.code} category={category} query={submittedQuery} role={role} onSelect={setSelectedRecord} />
      </Panel>
      {selectedRecord && <RecordDrawer item={selectedRecord} onClose={() => setSelectedRecord(null)} />}
    </div>
  )
}

function TaskDraftForm({ connectors, role, onCancel, onSaved }) {
  const [form, setForm] = useState({
    name: '', dimension: 'keyword', target_value: '', connector_id: connectors[0]?.id || '',
    frequency: '1h', history_days: 90, media_types: ['text'], languages: ['zh', 'en'],
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }))
  const toggle = (key, value) => setForm((current) => {
    const values = current[key].includes(value) ? current[key].filter((item) => item !== value) : [...current[key], value]
    return { ...current, [key]: values }
  })
  const submit = async (event) => {
    event.preventDefault()
    if (form.name.trim().length < 2 || !form.target_value.trim() || !form.connector_id) return
    setSaving(true); setError('')
    try {
      const payload = await api.post('/api/collection/tasks', { ...form, name: form.name.trim(), target_value: form.target_value.trim(), history_days: Number(form.history_days), role })
      onSaved(payload)
    } catch (submitError) { setError(submitError.message) }
    finally { setSaving(false) }
  }
  return (
    <Panel title="新建采集任务草稿" subtitle="配置采集条件；当前连接器均未完成授权，提交后只保存草稿" className="task-form-panel">
      <form className="task-form" onSubmit={submit}>
        <div className="form-grid">
          <label className="form-field"><span>任务名称</span><input required minLength="2" maxLength="120" value={form.name} onChange={(event) => update('name', event.target.value)} placeholder="输入便于识别的任务名称" /></label>
          <label className="form-field"><span>任务维度</span><select value={form.dimension} onChange={(event) => update('dimension', event.target.value)}><option value="person">人物</option><option value="account">账号</option><option value="keyword">关键词</option><option value="hashtag">话题标签</option></select></label>
          <label className="form-field form-span-2"><span>监测条件</span><input required maxLength="300" value={form.target_value} onChange={(event) => update('target_value', event.target.value)} placeholder="输入姓名、账号、关键词或话题标签" /></label>
          <label className="form-field"><span>连接器</span><select required value={form.connector_id} onChange={(event) => update('connector_id', event.target.value)}>{connectors.map((connector) => <option value={connector.id} key={connector.id}>{connector.name} · {statusLabel(connector.status)}</option>)}</select></label>
          <label className="form-field"><span>检查频率</span><select value={form.frequency} onChange={(event) => update('frequency', event.target.value)}><option value="15m">每 15 分钟</option><option value="30m">每 30 分钟</option><option value="1h">每小时</option><option value="6h">每 6 小时</option><option value="daily">每天</option></select></label>
          <label className="form-field"><span>历史范围（1-90 天）</span><input type="number" min="1" max="90" required value={form.history_days} onChange={(event) => update('history_days', event.target.value)} /></label>
          <div className="form-field"><span>媒体类型</span><div className="check-row">{[['text', '文本'], ['image', '图片'], ['video', '视频']].map(([value, label]) => <label key={value}><input type="checkbox" checked={form.media_types.includes(value)} onChange={() => toggle('media_types', value)} />{label}</label>)}</div></div>
          <div className="form-field form-span-2"><span>语言</span><div className="check-row">{[['zh', '中文'], ['en', '英文'], ['es', '西班牙语'], ['fr', '法语']].map(([value, label]) => <label key={value}><input type="checkbox" checked={form.languages.includes(value)} onChange={() => toggle('languages', value)} />{label}</label>)}</div></div>
        </div>
        <Notice tone="warning" icon={Server}>所选连接器未配置正式授权。保存后状态为“草稿”，不会触发采集，也不会显示运行指标。</Notice>
        {error && <Notice tone="danger" icon={CircleAlert}>{error}</Notice>}
        <div className="form-actions"><Button variant="secondary" onClick={onCancel}>取消</Button><Button type="submit" icon={Database} disabled={saving || form.name.trim().length < 2 || !form.target_value.trim() || !form.connector_id || !form.media_types.length || !form.languages.length}>{saving ? '保存中…' : '保存草稿'}</Button></div>
      </form>
    </Panel>
  )
}

function TaskList({ tasks, connectors, onNew, canCreate }) {
  const connectorMap = Object.fromEntries(connectors.map((connector) => [connector.id, connector]))
  if (!tasks.length) return <EmptyState icon={Database} title="尚未创建采集任务" description="可先保存任务草稿；连接器完成授权后再进入执行阶段。" />
  return <div className="task-list">{tasks.map((task) => { const connector = connectorMap[task.connector_id]; return <article key={task.id}><div className="task-status"><CircleDot size={17} /><Badge tone={statusTone(task.status)}>{statusLabel(task.status)}</Badge></div><div className="task-main"><div><strong>{task.name}</strong><span>#{task.id}</span></div><p>{TASK_DIMENSION_LABELS[task.dimension] || task.dimension} · {task.target_value}</p><div className="meta-line"><span>{connector?.name || task.connector_id}</span><span>{FREQUENCY_LABELS[task.frequency] || task.frequency}</span><span>历史 {task.history_days} 天</span><span>{(task.media_types || []).map((item) => MEDIA_LABELS[item] || item).join(' / ')}</span><span>{(task.languages || []).map((item) => LANGUAGE_LABELS[item] || item).join(' / ')}</span></div></div><div className="task-connector"><Badge>{statusLabel(connector?.status || 'NOT_CONFIGURED')}</Badge><span>连接器未配置，不会执行</span></div></article> })}<Button variant="secondary" icon={Database} disabled={!canCreate} title={!canCreate ? '研究员视角为只读' : ''} onClick={onNew}>继续新建任务</Button></div>
}

function DatasetRecords({ batchCode, category, query, role, onSelect }) {
  const search = new URLSearchParams({ role, limit: '200' })
  if (category) search.set('category', category)
  if (query) search.set('q', query)
  const state = useLoad(() => api.get(`/api/datasets/${batchCode}/records?${search}`), [batchCode, category, query, role])
  if (state.loading && !state.data) return <LoadingPanel label="正在读取批次记录…" />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const items = state.data?.items || []
  return <div className="record-browser"><div className="record-count">返回 {items.length} 条记录</div><div className="record-list">{items.map((item) => <button className="record-row" key={item.id} onClick={() => onSelect(item)}><span className="record-type"><Layers3 size={15} />{CATEGORY_LABELS[item.category] || item.category}</span><span className="record-copy"><strong>{item.title}</strong><small>{item.summary || '没有可展示摘要'}</small></span><Badge>{evidenceLabel(item.evidence_type)}</Badge><ChevronRight size={16} /></button>)}{!items.length && <EmptyState icon={FileSearch} title="没有匹配记录" description="请调整分类或关键词。" />}</div></div>
}

function RecordDrawer({ item, onClose }) {
  const content = item.content || {}; const sourceRefs = item.source_refs || []
  useEscapeClose(onClose)
  return <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer wide" role="dialog" aria-modal="true" aria-label="记录详情"><div className="drawer-header"><div><span>{CATEGORY_LABELS[item.category] || item.category}</span><h2>{item.title}</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭记录详情"><X size={19} /></button></div><div className="drawer-body"><div className="tag-list roomy"><Badge>{evidenceLabel(item.evidence_type)}</Badge><Badge>{item.sensitivity}</Badge><Badge>{item.id}</Badge></div><p className="lead-copy">{item.summary || '没有可展示摘要。'}</p><div className="detail-section"><h3>结构化字段</h3><div className="json-fields">{Object.entries(content).map(([key, value]) => <div key={key}><span>{key}</span><strong>{typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? '未提供')}</strong></div>)}</div></div><div className="detail-section"><h3>来源锚点</h3>{sourceRefs.length ? <div className="file-list">{sourceRefs.map((source) => /^https?:\/\//i.test(String(source)) ? <a className="source-link" href={source} target="_blank" rel="noreferrer" key={source}><ExternalLink size={14} />{shorten(source, 68)}</a> : <span key={source}><FileCheck2 size={14} />{source}</span>)}</div> : <p className="muted">源文件未提供可点击的原始 URL 或帖子 ID。</p>}</div></div></aside></div>
}

function KnowledgePage({ role, initialQuery }) {
  const collections = useLoad(() => api.get(`/api/knowledge/collections?role=${role}`), [role])
  const [query, setQuery] = useState(initialQuery); const [category, setCategory] = useState('')
  const [selectedRecord, setSelectedRecord] = useState(null)
  const [result, setResult] = useState(null); const [searching, setSearching] = useState(false); const [error, setError] = useState('')
  const runSearch = async (event) => {
    event?.preventDefault(); if (!query.trim()) return
    setSearching(true); setError('')
    try { setResult(await api.post('/api/knowledge/search', { query: query.trim(), role, top_k: 10, category: category || null })) }
    catch (searchError) { setError(searchError.message) }
    finally { setSearching(false) }
  }
  useEffect(() => setQuery(initialQuery), [initialQuery])
  useEffect(() => { if (initialQuery) runSearch() }, [initialQuery, role]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="page-stack">
      <PageHeader eyebrow="知识与研判 / KNOWLEDGE" title="嵌入式知识库" description="对本地结构化记录进行持久化索引、版本管理与可追溯混合检索。" />
      <Notice tone="info" icon={Fingerprint}><strong>检索能力边界：</strong> 当前融合词法匹配、规则和离线特征向量，不等同于大模型语义嵌入。</Notice>
      {collections.loading && !collections.data ? <LoadingPanel /> : collections.error ? <ErrorPanel message={collections.error} onRetry={collections.reload} /> : <div className="collection-grid">{(collections.data?.items || []).map((item) => <div className="knowledge-card" key={item.id}><div className="knowledge-icon"><BookOpenCheck size={21} /></div><div><span>知识集合</span><strong>{item.name}</strong><p>{item.description}</p></div><div className="knowledge-meta"><Badge tone="success">{statusLabel(item.version_status || item.lifecycle)}</Badge><span>{item.version}</span><span>{item.chunk_count} 个知识块</span></div></div>)}</div>}
      <Panel title="混合检索" subtitle="每条结果返回融合分数、证据性质和源文件引用">
        <form className="knowledge-search" onSubmit={runSearch}><div className="knowledge-search-box"><Search size={20} /><input autoFocus maxLength="500" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入人物、事件、主题或来源关键词" /></div><select value={category} onChange={(event) => setCategory(event.target.value)} aria-label="限制检索类别"><option value="">全部类别</option>{Object.entries(CATEGORY_LABELS).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select><Button type="submit" icon={Search} disabled={!query.trim() || searching}>{searching ? '检索中…' : '开始检索'}</Button></form>
        {error && <Notice tone="danger" icon={CircleAlert}>{error}</Notice>}
        {!result && !error && <EmptyState icon={Library} title="输入关键词开始检索" description="结果只来自当前本地知识集合，并附带证据引用。" />}
        {result && <div className="search-results"><div className="result-summary"><span>检索到 {result.result_count} 条结果</span><Badge>{result.retrieval_mode}</Badge><span>前两条分差 {result.margin}</span></div><Notice tone="subtle">{result.notice}</Notice>{(result.results || []).map((item, index) => <SearchResult key={item.record_id} item={item} index={index + 1} onSelect={async (record) => { try { setSelectedRecord(await api.get(`/api/records/${encodeURIComponent(record.record_id)}?role=${role}`)) } catch { setSelectedRecord(record) } }} />)}{!result.results?.length && <EmptyState icon={Search} title="没有足够相关的记录" description="调整关键词或取消类别限制后重试。" />}</div>}
        {selectedRecord && <RecordDrawer item={selectedRecord} onClose={() => setSelectedRecord(null)} />}
      </Panel>
    </div>
  )
}

function SearchResult({ item, index, onSelect }) {
  const score = Math.round((item.score || 0) * 100)
  return <article className="search-result-card clickable-card" role="button" tabIndex="0" onClick={() => onSelect?.(item)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect?.(item) } }}><div className="result-index">{String(index).padStart(2, '0')}</div><div className="result-content"><div className="result-title"><div><Badge>{item.category_label}</Badge><Badge>{evidenceLabel(item.evidence_type)}</Badge></div><strong>{item.title}</strong></div><p>{item.summary || '没有可展示摘要。'}</p><div className="citation-line"><FileCheck2 size={14} /><span>{(item.source_refs || []).join(' · ') || '未提供源文件锚点'}</span><span>{item.batch_code}</span><span>{item.source_date}</span><span title={item.content_hash}>哈希 {shorten(item.content_hash, 14)}</span></div></div><div className="score-block"><strong>{score}</strong><span>融合分</span><div className="score-track"><i style={{ width: `${score}%` }} /></div><small>词法 {Math.round((item.score_breakdown?.lexical || 0) * 100)} · 向量 {Math.round((item.score_breakdown?.offline_vector || 0) * 100)} · 规则 {Math.round((item.score_breakdown?.rule || 0) * 100)}</small></div></article>
}

function GraphPage({ role }) {
  const [view, setView] = useState('actors'); const [selected, setSelected] = useState(null)
  const state = useLoad(() => api.get(`/api/graph?view=${view}&role=${role}`), [view, role])
  useEffect(() => setSelected(null), [view, role])
  return (
    <div className="page-stack graph-page">
      <PageHeader eyebrow="知识与研判 / GRAPH" title="有向知识图谱" description="按人物、事件、传播和数据血缘四种视图检查关系及证据边界。" />
      <div className="view-tabs" role="tablist">{GRAPH_VIEWS.map((item) => <button key={item.id} role="tab" aria-selected={view === item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}><strong>{item.label}</strong><span>{item.description}</span></button>)}</div>
      {state.loading && !state.data ? <LoadingPanel label="正在构建图谱视图…" /> : state.error ? <ErrorPanel message={state.error} onRetry={state.reload} /> : <><Notice tone="info" icon={Network}>{state.data.notice}</Notice><div className="graph-layout"><Panel className="graph-panel"><div className="graph-toolbar"><div><Badge tone="success">有向图</Badge><span>{state.data.stats.node_count} 个节点</span><span>{state.data.stats.edge_count} 条关系</span>{state.data.stats.hidden_restricted > 0 && <span>隐藏 {state.data.stats.hidden_restricted} 个受限节点</span>}</div><div className="graph-legend">{Object.keys(state.data.stats.node_types || {}).map((type) => <span key={type}><i style={{ background: NODE_COLORS[type] || '#64748b' }} />{type} {state.data.stats.node_types[type]}</span>)}</div></div><GraphCanvas data={state.data} selected={selected} onSelect={setSelected} /></Panel><GraphDetail node={selected} edges={state.data.edges || []} nodes={state.data.nodes || []} /></div></>}
    </div>
  )
}

function GraphCanvas({ data, selected, onSelect }) {
  const nodeMap = useMemo(() => Object.fromEntries((data.nodes || []).map((node) => [node.id, node])), [data.nodes])
  const point = (node) => ({ x: 70 + (node.x || 50) * 8.6, y: 32 + (node.y || 50) * 5.35 })
  return <div className="graph-canvas-wrap"><svg className="graph-canvas" viewBox="0 0 1000 620" role="img" aria-label={`${data.view} 有向知识图谱`}><defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 z" fill="#9aa8b8" /></marker></defs><g className="graph-edges">{(data.edges || []).map((edge) => { const source = nodeMap[edge.source]; const target = nodeMap[edge.target]; if (!source || !target) return null; const a = point(source); const b = point(target); return <line key={edge.id} x1={a.x} y1={a.y} x2={b.x} y2={b.y} markerEnd="url(#arrowhead)"><title>{edge.label} · {evidenceLabel(edge.evidence)}</title></line> })}</g><g className="graph-nodes">{(data.nodes || []).map((node) => { const p = point(node); const active = selected?.id === node.id; const radius = Math.max(7, node.size || 8); return <g key={node.id} className={`graph-node ${active ? 'active' : ''}`} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex="0" onClick={() => onSelect(node)} onKeyDown={(event) => (event.key === 'Enter' || event.key === ' ') && onSelect(node)}><circle r={radius + (active ? 3 : 0)} fill={NODE_COLORS[node.type] || '#64748b'} />{(node.size >= 9 || active) && <text y={radius + 14} textAnchor="middle">{shorten(node.label, 16)}</text>}<title>{node.label} · {node.subtitle}</title></g> })}</g></svg></div>
}

function GraphDetail({ node, edges, nodes }) {
  if (!node) return <aside className="graph-detail empty"><Network size={26} /><strong>选择节点查看详情</strong><span>可检查节点属性、证据性质和相邻关系。</span></aside>
  const nodeMap = Object.fromEntries(nodes.map((item) => [item.id, item])); const related = edges.filter((edge) => edge.source === node.id || edge.target === node.id)
  return <aside className="graph-detail"><div className="graph-detail-head"><span className="node-dot" style={{ background: NODE_COLORS[node.type] || '#64748b' }} /><div><span>{node.type}</span><h2>{node.label}</h2><p>{node.subtitle || '没有补充说明'}</p></div></div><DefinitionList items={[["敏感级别", node.sensitivity], ['证据性质', evidenceLabel(node.evidence)], ['相邻关系', `${related.length} 条`]]} /><div className="detail-section"><h3>关系明细</h3><div className="edge-list">{related.map((edge) => { const outbound = edge.source === node.id; const other = nodeMap[outbound ? edge.target : edge.source]; return <div key={edge.id}><span>{outbound ? '出边' : '入边'}</span><strong>{edge.label}</strong><small>{other?.label || '未知节点'}</small><Badge>{evidenceLabel(edge.evidence)}</Badge></div> })}{!related.length && <span className="muted">没有相邻关系</span>}</div></div><div className="detail-section"><h3>节点元数据</h3><pre>{JSON.stringify(node.metadata || {}, null, 2)}</pre></div></aside>
}

function AnalysisPage({ role }) {
  const state = useLoad(() => api.get(`/api/analysis?role=${role}`), [role])
  if (state.loading && !state.data) return <LoadingPanel />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const data = state.data; const maxTopic = Math.max(...(data.topics || []).map((item) => item.count), 1)
  return <div className="page-stack"><PageHeader eyebrow="知识与研判 / ANALYSIS" title="分析研判" description="基于当前批次的来源标签和证据类型进行统计，不将未运行模型包装成分析结论。" /><div className="analysis-status-grid"><div className="analysis-status not-run"><div><CircleAlert size={20} /><Badge tone="warning">{statusLabel(data.sentiment.status)}</Badge></div><strong>情感分析</strong><p>{data.sentiment.reason}</p></div><div className="analysis-status source-tags"><div><CheckCircle2 size={20} /><Badge tone="success">已统计</Badge></div><strong>主题标签</strong><p>使用源文件已有主题标签聚合，未自动扩写或推断。</p></div><div className="analysis-status evidence"><div><CheckCircle2 size={20} /><Badge tone="success">已统计</Badge></div><strong>证据分层</strong><p>按记录的证据性质展示分布，支持进一步人工复核。</p></div></div><Notice tone="info">{data.notice}</Notice><div className="two-column wide-left"><Panel title="主题标签分布" subtitle="来自源文件标签的出现次数"><div className="topic-bars">{(data.topics || []).map((item) => <div key={item.name}><span title={item.name}>{item.name}</span><div><i style={{ width: `${(item.count / maxTopic) * 100}%` }} /></div><strong>{item.count}</strong></div>)}{!data.topics?.length && <EmptyState icon={Layers3} title="没有主题标签" />}</div></Panel><Panel title="风险与证据概况" subtitle="事件风险字段和记录证据性质"><div className="summary-section"><h3>事件风险标注</h3><div className="stat-chip-row">{(data.risks || []).map((item) => <div key={item.level}><span>{item.level}</span><strong>{item.count}</strong></div>)}{!data.risks?.length && <span className="muted">未形成风险统计</span>}</div></div><div className="summary-section"><h3>证据性质</h3><div className="evidence-list">{(data.evidence || []).map((item) => <div key={item.type}><span>{evidenceLabel(item.type)}</span><strong>{item.count}</strong></div>)}</div></div></Panel></div></div>
}

function AlertsPage({ role }) {
  const state = useLoad(() => api.get(`/api/alerts?role=${role}`), [role])
  const [selected, setSelected] = useState(null)
  const [message, setMessage] = useState('')
  if (state.loading && !state.data) return <LoadingPanel />
  if (state.error) return <ErrorPanel message={state.error} onRetry={state.reload} />
  const isPublic = state.data.mode === 'PUBLIC_WEB_SAMPLE'
  return <div className="page-stack"><PageHeader eyebrow="知识与研判 / ALERTS" title="风险线索" description={isPublic ? '公开样本只展示经规则或人工确认的风险线索；未运行模型时不生成虚构结论。' : '展示测试批次中的规则命中，供人工核验；不是实时预警或已核实结论。'} /><Notice tone="warning" icon={AlertTriangle}><strong>{isPublic ? '分析边界。' : '测试规则命中。'}</strong> {state.data.notice}</Notice>{message && <Notice tone="info" icon={CheckCircle2}>{message}</Notice>}<div className="alert-list">{(state.data.items || []).map((item) => <article className="alert-card" key={item.id}><div className="alert-level"><span>{String(item.risk).toLowerCase() === 'high' ? '高' : item.risk}</span><small>{item.trigger}</small></div><div className="alert-copy"><div><Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge><span>{item.date || '日期未提供'}</span><span>{evidenceLabel(item.evidence)}</span>{item.assignee && <span>负责人：{item.assignee}</span>}</div><h2>{item.title}</h2><p>{item.summary}</p>{item.note && <div className="alert-note"><strong>核验备注</strong><span>{item.note}</span></div>}<div className="citation-line"><FileCheck2 size={14} />{(item.sources || []).join(' · ') || '来源锚点未提供'}</div></div><Button variant="secondary" disabled={role !== 'core'} title={role !== 'core' ? '研究员视角为只读' : ''} onClick={() => setSelected(item)}>核验处置</Button></article>)}{!state.data.items?.length && <EmptyState icon={BellRing} title="没有风险线索" description={isPublic ? '当前公开样本未产生经确认的风险线索。' : '当前权限视角下没有需要展示的测试线索。'} />}</div>{selected && role === 'core' && <AlertActionDrawer item={selected} role={role} onClose={() => setSelected(null)} onSaved={(payload) => { setSelected(null); setMessage(`线索 ${payload.id} 已更新为“${statusLabel(payload.status)}”。`); state.reload() }} />}</div>
}

function AlertActionDrawer({ item, role, onClose, onSaved }) {
  useEscapeClose(onClose)
  const [form, setForm] = useState({ status: item.status || 'PENDING', assignee: item.assignee || '', note: item.note || '' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event) => {
    event.preventDefault(); setSaving(true); setError('')
    try {
      const payload = await api.patch(`/api/alerts/${encodeURIComponent(item.id)}`, { status: form.status, assignee: form.assignee.trim(), note: form.note.trim(), role })
      onSaved(payload)
    } catch (submitError) { setError(submitError.message) }
    finally { setSaving(false) }
  }
  return <div className="drawer-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><aside className="drawer" role="dialog" aria-modal="true" aria-label="风险线索核验处置"><div className="drawer-header"><div><span>测试规则命中 · {item.id}</span><h2>核验处置</h2></div><button className="icon-btn" onClick={onClose} aria-label="关闭核验处置"><X size={19} /></button></div><form className="drawer-body action-form" onSubmit={submit}><div className="action-subject"><Badge tone="warning">{item.risk}</Badge><strong>{item.title}</strong><p>{item.summary}</p></div><label className="form-field"><span>处置状态</span><select value={form.status} onChange={(event) => setForm((current) => ({ ...current, status: event.target.value }))}><option value="PENDING">待处理</option><option value="ACKNOWLEDGED">已确认</option><option value="RESOLVED">已完成</option></select></label><label className="form-field"><span>负责人</span><input maxLength="80" value={form.assignee} onChange={(event) => setForm((current) => ({ ...current, assignee: event.target.value }))} placeholder="填写核验负责人" /></label><label className="form-field"><span>核验备注</span><textarea maxLength="1000" rows="7" value={form.note} onChange={(event) => setForm((current) => ({ ...current, note: event.target.value }))} placeholder="记录核验依据、判断和后续动作" /></label><Notice tone="info" icon={Info}>处置只更新测试线索工作流状态，不会把规则命中自动标记为已核实事实。</Notice>{error && <Notice tone="danger" icon={CircleAlert}>{error}</Notice>}<div className="form-actions"><Button variant="secondary" onClick={onClose}>取消</Button><Button type="submit" icon={CheckCircle2} disabled={saving}>{saving ? '提交中…' : '提交处置'}</Button></div></form></aside></div>
}

function ReportsPage({ role }) {
  const templates = useLoad(() => api.get('/api/reports/templates'), [])
  const [template, setTemplate] = useState('validation'); const [focus, setFocus] = useState(''); const [report, setReport] = useState(null); const [generating, setGenerating] = useState(false); const [error, setError] = useState('')
  const generate = async () => { setGenerating(true); setError(''); try { setReport(await api.post('/api/reports/generate', { template, focus, role })) } catch (generateError) { setError(generateError.message) } finally { setGenerating(false) } }
  const download = () => {
    if (!report) return
    const sections = report.sections.map((section) => `## ${section.title}\n\n${section.content}`).join('\n\n')
    const citations = report.citations?.length ? `\n\n## 引用\n\n${report.citations.map((item, index) => `${index + 1}. ${item.title}｜${(item.source_refs || []).join('、')}｜${evidenceLabel(item.evidence_type)}｜SHA-256 ${item.content_hash || '未提供'}`).join('\n')}` : ''
    const content = `# ${report.title}\n\n> ${report.notice}\n\n${sections}${citations}\n`
    const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${report.title.replace(/[\\/:*?\"<>|]/g, '-')}.md`; anchor.click(); URL.revokeObjectURL(url)
  }
  if (templates.loading && !templates.data) return <LoadingPanel />
  if (templates.error) return <ErrorPanel message={templates.error} onRetry={templates.reload} />
  const publicReport = report?.status === 'GENERATED_FROM_PUBLIC_WEB_SAMPLE'
  return <div className="page-stack"><PageHeader eyebrow="输出与治理 / REPORTS" title="报告中心" description="按当前活动批次生成结构化报告，并保留检索引用和证据边界。" /><div className="report-layout"><Panel title="生成设置" subtitle="报告由结构化规则生成，不调用生成式大模型"><div className="template-grid">{(templates.data.items || []).map((item) => <button key={item.id} className={template === item.id ? 'active' : ''} onClick={() => setTemplate(item.id)}><FileText size={18} /><div><strong>{item.name}</strong><span>{item.description}</span></div>{template === item.id && <Check size={16} />}</button>)}</div><label className="form-field"><span>检索焦点（可选）</span><input maxLength="500" value={focus} onChange={(event) => setFocus(event.target.value)} placeholder="例如人物、事件或主题关键词" /></label><Button icon={FileStack} onClick={generate} disabled={generating}>{generating ? '生成中…' : '生成报告'}</Button>{error && <Notice tone="danger">{error}</Notice>}</Panel><Panel title="报告预览" subtitle={report ? report.status : '尚未生成'} action={report && <Button variant="secondary" icon={ArrowDownToLine} onClick={download}>下载 Markdown</Button>}>{!report ? <EmptyState icon={FileText} title="选择模板并生成报告" description="生成结果将出现在这里。" /> : <article className="report-preview"><div className="report-cover"><span>{publicReport ? '公开网页样本报告' : '结构化测试报告'}</span><h2>{report.title}</h2><p>{report.notice}</p></div>{report.sections.map((section) => <section key={section.title}><h3>{section.title}</h3><p>{section.content}</p></section>)}{report.citations?.length > 0 && <section><h3>引用记录</h3>{report.citations.map((item, index) => <div className="report-citation" key={item.record_id}><span>{index + 1}</span><div><strong>{item.title}</strong><small>{(item.source_refs || []).join(' · ')} · {evidenceLabel(item.evidence_type)} · SHA-256 {shorten(item.content_hash, 16)}</small></div></div>)}</section>}</article>}</Panel></div></div>
}

function ObjectsPage({ role }) {
  const state = useLoad(() => api.get(`/api/monitor-objects?role=${role}`), [role])
  const [layer, setLayer] = useState('')
  const layers = ['第一层', '第二层A', '第二层B', '第三层']
  const items = (state.data?.items || []).filter((o) => !layer || o.layer === layer)
  return (
    <div className="page-stack">
      <PageHeader eyebrow="监测与数据 / OBJECTS" title="监测对象分层" description="按需求表四层对象体系管理监测对象；第一层最高敏感级，仅核心课题组可见。" />
      {state.loading && <LoadingPanel label="正在读取监测对象…" />}
      {state.error && <ErrorPanel message={state.error} onRetry={state.reload} />}
      {state.data && (
        <>
          <div className="filter-row">
            <button className={!layer ? 'active' : ''} onClick={() => setLayer('')}>全部</button>
            {layers.map((l) => <button key={l} className={layer === l ? 'active' : ''} onClick={() => setLayer(l)}>{l}</button>)}
          </div>
          {layers.map((l) => {
            const arr = items.filter((o) => o.layer === l)
            if (!arr.length) return null
            return (
              <Panel key={l} title={`${l} · ${arr.length} 个对象`} subtitle={l === '第一层' ? '最高敏感级，仅核心课题组可见' : '实名开放，全所科研人员可查看'}>
                <div className="object-grid">
                  {arr.map((o) => (
                    <div key={o.id} className="object-card">
                      <strong>{o.name}</strong>
                      <span>{o.title || '—'}</span>
                      <span>{o.account || '无账号'}{o.organization ? ` · ${o.organization}` : ''}</span>
                      <div className="meta-line">
                        <Badge>{o.object_type}</Badge>
                        <Badge tone={o.sensitivity === '最高敏感级' ? 'danger' : 'neutral'}>{o.sensitivity}</Badge>
                        {o.influence && <Badge tone="neutral">影响力 {o.influence}</Badge>}
                      </div>
                      {(o.anomalyFlags || []).length > 0 && (
                        <div className="meta-line">{o.anomalyFlags.map((f) => <Badge key={f} tone="warning">{f}</Badge>)}</div>
                      )}
                    </div>
                  ))}
                </div>
              </Panel>
            )
          })}
        </>
      )}
    </div>
  )
}

function OpinionPage({ role }) {
  const overview = useLoad(() => api.get(`/api/opinion/overview?role=${role}`), [role])
  const [topic, setTopic] = useState('')
  const [sentiment, setSentiment] = useState('')
  const [risk, setRisk] = useState('')
  const query = new URLSearchParams({ role, topic, sentiment, risk_grade: risk }).toString()
  const items = useLoad(() => api.get(`/api/opinion/items?${query}`), [role, topic, sentiment, risk])
  const ov = overview.data || {}
  const bar = (label, value, max) => ({ label, value, width: max ? `${Math.max(2, Math.round(value / max * 100))}%` : '0%' })
  return (
    <div className="page-stack">
      <PageHeader eyebrow="知识与研判 / OPINION" title="舆情分析" description="涉华内容按核心议题、情感（正/中/负）、风险分级（紧急/高危/中等/低危）统计。" />
      {overview.loading && <LoadingPanel label="正在汇总舆情分析…" />}
      {overview.error && <ErrorPanel message={overview.error} onRetry={overview.reload} />}
      {overview.data && (
        <>
          <div className="two-column wide-left">
            <Panel title="核心议题分布" subtitle={`共 ${ov.item_count} 条涉华内容`}>
              <div className="topic-bars">
                {Object.entries(ov.topics || {}).sort((a, b) => b[1] - a[1]).map(([t, n]) => {
                  const max = Math.max(...Object.values(ov.topics || {}))
                  const b = bar(t, n, max)
                  return <div key={t}><span>{t}</span><div><i style={{ width: b.width }} /></div><strong>{n}</strong></div>
                })}
              </div>
            </Panel>
            <div className="panel-stack">
              <Panel title="情感分布" subtitle="正面 / 中性 / 负面">
                <div className="topic-bars">
                  {['负面', '中性', '正面'].map((s) => <div key={s}><span>{s}</span><div><i style={{ width: `${(ov.sentiments?.[s] || 0) * 3}%` }} /></div><strong>{ov.sentiments?.[s] || 0}</strong></div>)}
                </div>
              </Panel>
              <Panel title="风险分级" subtitle="紧急 / 高危 / 中等 / 低危 / 无">
                <div className="topic-bars">
                  {['紧急', '高危', '中等', '低危', '无'].map((r) => <div key={r}><span>{r}</span><div><i style={{ width: `${(ov.risk_grades?.[r] || 0) * 5}%` }} /></div><strong>{ov.risk_grades?.[r] || 0}</strong></div>)}
                </div>
              </Panel>
            </div>
          </div>
          <Panel title="涉华内容明细" subtitle={`共 ${items.data?.count ?? 0} 条`}>
            <div className="filter-row">
              <select value={topic} onChange={(e) => setTopic(e.target.value)}>
                <option value="">全部议题</option>
                {Object.keys(ov.topics || {}).map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <select value={sentiment} onChange={(e) => setSentiment(e.target.value)}>
                <option value="">全部情感</option>
                {['正面', '中性', '负面'].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select value={risk} onChange={(e) => setRisk(e.target.value)}>
                <option value="">全部风险</option>
                {['紧急', '高危', '中等', '低危', '无'].map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            <div className="alert-list">
              {(items.data?.items || []).map((it) => (
                <article className="alert-card" key={it.id}>
                  <div className="alert-copy">
                    <div>
                      <Badge>{it.topic}</Badge>
                      <Badge tone={it.sentiment === '负面' ? 'danger' : it.sentiment === '正面' ? 'success' : 'neutral'}>{it.sentiment}</Badge>
                      <Badge tone={it.risk_grade === '紧急' || it.risk_grade === '高危' ? 'danger' : it.risk_grade === '低危' || it.risk_grade === '中等' ? 'warning' : 'neutral'}>{it.risk_grade}</Badge>
                    </div>
                    <h2>{it.title}</h2>
                    <p>{it.summary}</p>
                    <div className="meta-line"><span>{it.object_name} · {it.layer}</span><span>{it.source}</span><span>{it.published_at}</span></div>
                  </div>
                </article>
              ))}
              {!items.data?.items?.length && <EmptyState icon={Search} title="没有匹配内容" description="请调整筛选条件。" />}
            </div>
          </Panel>
        </>
      )}
    </div>
  )
}

function ChatPage({ role }) {
  const [messages, setMessages] = useState([{ id: 1, type: 'system', text: '这里是检索式问答。回答仅根据当前活动知识库组织，并返回可核查引用。' }])
  const [query, setQuery] = useState(''); const [sending, setSending] = useState(false); const endRef = useRef(null)
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, sending])
  const send = async (event) => {
    event.preventDefault(); const value = query.trim(); if (!value || sending) return
    const id = Date.now(); setMessages((current) => [...current, { id, type: 'user', text: value }]); setQuery(''); setSending(true)
    try { const response = await api.post('/api/chat', { query: value, role, top_k: 5 }); setMessages((current) => [...current, { id: id + 1, type: 'assistant', text: response.answer, citations: response.citations, notice: response.notice }]) }
    catch (error) { setMessages((current) => [...current, { id: id + 1, type: 'error', text: error.message }]) }
    finally { setSending(false) }
  }
  return <div className="page-stack chat-page"><PageHeader eyebrow="输出与治理 / RETRIEVAL QA" title="检索式问答" description="基于本地知识索引返回规则化摘要；未连接大模型，不提供生成式推理。" /><Notice tone="info" icon={Fingerprint}>使用本地词法、规则和离线特征向量检索。回答内容应结合引用证据性质人工复核。</Notice><div className="chat-shell"><div className="chat-messages">{messages.map((message) => <div className={`chat-message ${message.type}`} key={message.id}><div className="chat-avatar">{message.type === 'user' ? <UserRound size={17} /> : message.type === 'error' ? <CircleAlert size={17} /> : <Library size={17} />}</div><div className="chat-bubble"><p>{message.text}</p>{message.notice && <small>{message.notice}</small>}{message.citations?.length > 0 && <div className="chat-citations"><strong>引用</strong>{message.citations.map((item, index) => <div key={item.record_id}><span>{index + 1}</span><div><b>{item.title}</b><small>{(item.source_refs || []).join(' · ') || '来源锚点未提供'} · {evidenceLabel(item.evidence_type)}</small></div></div>)}</div>}</div></div>)}{sending && <div className="chat-message assistant"><div className="chat-avatar"><Library size={17} /></div><div className="chat-bubble loading-dots"><i /><i /><i /></div></div>}<div ref={endRef} /></div><form className="chat-composer" onSubmit={send}><textarea maxLength="500" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(event) } }} placeholder="询问当前数据批次中的人物、事件、主题或来源…" rows="2" /><Button type="submit" icon={Send} disabled={!query.trim() || sending}>发送</Button></form></div></div>
}

function DefinitionList({ items }) { return <dl className="definition-list">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? '未提供'}</dd></div>)}</dl> }
function EmptyState({ icon: Icon, title, description }) { return <div className="empty-state"><Icon size={25} /><strong>{title}</strong>{description && <span>{description}</span>}</div> }

export default App
