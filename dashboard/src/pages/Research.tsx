import { useMemo, useState } from 'react'
import { evidenceMode, useEvidenceMode, useResource } from '../api/client'
import { unavailableReason } from '../api/phaseB'
import type { Presence, ReportList, ReportSummary } from '../api/phaseB'
import { CandlestickChart } from '../components/CandlestickChart'
import type { CandleBar } from '../components/CandlestickChart'
import { Badge, Drawer, Empty, MetricGrid, Panel, Pnl, ResourceState, Table } from '../components/ui'
import { finite, kst, money, numeric } from '../utils/formatters'
import { useRouteQuery } from '../utils/routeQuery'
import './research.css'

type Amount = number | string | null
type Report = ReportSummary & {
  score_formula?: string
  evaluations?: Evaluation[]; candidates?: Evaluation[]
}
type NewsReference = { title?: string; url?: string | null; published_at?: string; description?: string; is_risk_signal?: boolean }
type Evaluation = {
  symbol: string; name?: string; action_decision?: string; label?: string; rank?: number
  financial_score?: Amount; news_score?: Amount; risk_penalty?: Amount; total_score?: Amount
  financial_breakdown?: Record<string, { points?: Amount; maximum?: Amount; status?: string; reason?: string }>
  news_events?: { event_id: string; title?: string; points?: Amount; status?: string; score_target?: string }[]
  financial_source_url?: string | null
  recent_headlines?: NewsReference[]; risk_headlines?: NewsReference[]; recent_dart_filings?: NewsReference[]
  reasons?: string[]; warnings?: string[]; pending_checks?: string[]; advisories?: string[]
  recent_news_complete?: boolean; risk_history_complete?: boolean
  price?: { status?: string; strategy?: string; session?: string; reasons?: string[];
    entry_conditions?: string[]; invalidation_conditions?: string[] }
}
type TradePlan = {
  grade_label?: string; reasons?: string[]; annotations?: string[]
  breakout_price?: number | null; support_price?: number | null; stop_loss?: number | null
}
type ChartResponse = Presence & {
  bars?: CandleBar[]; support?: number[]; resistance?: number[]; analysis_available?: boolean
  analysis?: { trade_plan?: TradePlan; patterns?: { primary?: { name_ko?: string } | null } } | null
  technical?: { methods?: { code: string; name: string; reading: string; limit: string }[]; limits?: string[] } | null
}

const reportsPath = '/api/research/reports'
const pageSize = 100
const verdicts: Record<string, { label: string; tone: 'ok' | 'warn' | 'danger' | 'unknown' }> = {
  BUY_RECOMMENDATION: { label: '조건부 추천', tone: 'ok' },
  WATCH: { label: '관찰', tone: 'unknown' },
  VERIFY_PENDING: { label: '검증 보류', tone: 'warn' },
  EXCLUDED: { label: '제외', tone: 'danger' },
  NOT_APPLICABLE: { label: '평가 대상 아님', tone: 'unknown' },
}
const setups: Record<string, string> = {
  BOTTOM_REBOUND: '바닥 반등', MOMENTUM_CONTINUATION: '모멘텀 지속', WAIT_FOR_SETUP: '셋업 대기',
}
const financialLabels: Record<string, string> = {
  profit: '수익성', cash_flow: '영업현금흐름', profit_history: '이익 이력', turnover: '거래대금', free_cash_flow: '잉여현금흐름',
}
const missingReasons: Record<string, string> = {
  INVALID_REPORT_ID: '보고서 ID 형식을 확인해 주세요.',
  INVALID_SYMBOL: '종목 코드 형식을 확인할 수 없습니다.',
  SYMBOL_NOT_IN_REPORT: '선택 보고서에 해당 종목의 기록이 없습니다.',
  BARS_NOT_FOUND: '보고서 기준으로 표시할 저장 일봉이 없습니다.',
  INVALID_PRICE_SESSION: '보고서의 가격 기준 거래일이 올바르지 않습니다.',
  PRICE_SESSION_INCOMPLETE: '보고서 기준 거래일의 저장 일봉이 누락됐습니다.',
  REPORT_PRICE_INPUT_MISMATCH: '저장 일봉이 보고서에 사용된 가격 기록과 일치하지 않습니다.',
}

function researchUnavailable(reason: string | undefined, fallback: string) {
  return missingReasons[reason ?? ''] ?? unavailableReason(reason, fallback)
}

function Verdict({ row }: { row: Evaluation }) {
  const verdict = row.action_decision ? verdicts[row.action_decision] : undefined
  return <Badge tone={verdict?.tone}>{row.label ?? verdict?.label ?? row.action_decision ?? '판정 확인 불가'}</Badge>
}

function EvidenceList({ items, empty }: { items?: string[]; empty: string }) {
  return items?.length ? <ul className="research-evidence-list">
    {items.map((item, index) => <li key={index}>{item}</li>)}
  </ul> : <Empty>{empty}</Empty>
}

function safeReference(value?: string | null) {
  if (!value) return null
  try {
    const url = new URL(value)
    return (url.protocol === 'https:' || url.protocol === 'http:') && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

function References({ label, items }: { label: string; items?: NewsReference[] }) {
  if (!items?.length) return null
  return <details>
    <summary>{label} ({items.length})</summary>
    <ul className="evidence-list">
      {items.map((item, index) => {
        const url = safeReference(item.url)
        return <li key={index}>
          {url ? <a className="action-link" href={url} target="_blank" rel="noreferrer">{item.title || '자료 원문'}</a>
            : <strong>{item.title || '제목 확인 불가'}</strong>}
          {item.published_at && <p className="muted mono">{kst(item.published_at)}</p>}
          {item.description && <p>{item.description}</p>}
        </li>
      })}
    </ul>
  </details>
}

function ResearchChart({ row, reportId }: { row: Evaluation; reportId: string }) {
  const path = '/api/research/chart/' + encodeURIComponent(row.symbol) + '?report_id=' + encodeURIComponent(reportId)
  const { data, loading, error } = useResource<ChartResponse>(path)
  const plan = data?.analysis?.trade_plan
  return <section className="stack research-detail-section" aria-label="저장 일봉과 기술 분석">
    <h3>저장 일봉과 기술 분석</h3>
    <ResourceState loading={loading} error={error} />
    {data && (data.present && data.bars?.length
      ? <CandlestickChart bars={data.bars} support={data.support} resistance={data.resistance} />
      : <Empty>{researchUnavailable(data.reason, missingReasons.BARS_NOT_FOUND)}</Empty>)}
    {data?.present && data.analysis_available === false && <p className="muted">상세 기술 판독에 필요한 일봉이 부족합니다.</p>}
    {plan && <div className="stack">
      <p><strong>{plan.grade_label ?? '차트 판독'}</strong>
        {data?.analysis?.patterns?.primary?.name_ko && <> · {data.analysis.patterns.primary.name_ko}</>}
      </p>
      <MetricGrid metrics={[
        { label: '돌파 가격', value: money(plan.breakout_price) }, { label: '지지 가격', value: money(plan.support_price) },
        { label: '차트 무효화 가격', value: money(plan.stop_loss) },
      ]} />
      <EvidenceList items={[...(plan.reasons ?? []), ...(plan.annotations ?? [])]} empty="저장된 차트 판독 근거가 없습니다." />
    </div>}
    {data?.technical?.methods?.length ? <details>
      <summary>기술 지표 판독 근거</summary>
      <dl className="research-readout stack">{data.technical.methods.map(method => <div key={method.code}>
        <dt>{method.name}</dt><dd>{method.reading}</dd><dd className="muted">{method.limit}</dd>
      </div>)}</dl>
      {data.technical.limits?.map(limit => <p className="muted" key={limit}>{limit}</p>)}
    </details> : null}
  </section>
}

function ResearchDetails({ row, reportId, close }: { row: Evaluation; reportId: string; close: () => void }) {
  const financial = Object.entries(row.financial_breakdown ?? {})
  const financialSource = safeReference(row.financial_source_url)
  const coverage = (covered?: boolean) => covered === true ? '확인 완료' : covered === false ? '미완료' : '확인 불가'
  return <Drawer title={<>{row.name ? `${row.name} ` : ''}<span className="mono">{row.symbol}</span></>} onClose={close}>
    <div className="cluster"><Verdict row={row} /><span className="muted mono">{reportId}</span></div>
    <MetricGrid metrics={[
      { label: '재무 (70%)', value: numeric(row.financial_score) }, { label: '뉴스 (30%)', value: numeric(row.news_score) },
      { label: '리스크 차감', value: numeric(row.risk_penalty) }, { label: '총점', value: numeric(row.total_score) },
    ]} />
    <ResearchChart row={row} reportId={reportId} />
    <section className="stack research-detail-section" aria-label="재무 점수 분해">
      <h3>재무 점수 분해</h3>
      {financial.length ? <Table label="재무 평가 항목" columns={['항목', '점수 / 배점', '근거']}>
        {financial.map(([key, item]) => <tr key={key}><th scope="row">{financialLabels[key] ?? key}</th>
          <td className="mono">{numeric(item.points)} / {numeric(item.maximum)}</td>
          <td>{item.reason ?? (item.status === 'AVAILABLE' ? '근거 기록 없음' : '확인 불가')}</td>
        </tr>)}
      </Table> : <Empty>저장된 재무 평가 항목이 없습니다.</Empty>}
      {financialSource && <a className="action-link" href={financialSource} target="_blank" rel="noreferrer">재무 근거 원문</a>}
    </section>
    <section className="stack research-detail-section" aria-label="뉴스·리스크 근거">
      <h3>뉴스·리스크 근거</h3>
      <p className="muted">최근 뉴스 검토 {coverage(row.recent_news_complete)} · 과거 리스크 검토 {coverage(row.risk_history_complete)}</p>
      {row.news_events?.length ? <Table label="뉴스 이벤트 반영" columns={['이벤트', '반영', '점수', '상태']}>
        {row.news_events.map(event => <tr key={event.event_id}>
          <th scope="row">{event.title ?? event.event_id}</th><td>{event.score_target === 'risk_penalty' ? '리스크' : '뉴스'}</td>
          <td className="mono"><Pnl value={finite(event.points)} /></td><td>{event.status ?? '확인 불가'}</td>
        </tr>)}
      </Table> : <Empty>저장된 뉴스 이벤트가 없습니다.</Empty>}
      <References label="최근 기사" items={row.recent_headlines} />
      <References label="리스크 기사" items={row.risk_headlines} />
      <References label="DART 공시" items={row.recent_dart_filings} />
      <h4>판정 사유·경고</h4>
      <EvidenceList items={[...new Set([...(row.reasons ?? []), ...(row.warnings ?? []), ...(row.price?.reasons ?? [])])]}
        empty="저장된 판정 사유·경고가 없습니다." />
    </section>
    <section className="stack research-detail-section" aria-label="보류 점검과 자문">
      <h3>보류 점검과 자문</h3>
      <h4>보류 점검</h4><EvidenceList items={row.pending_checks} empty="저장된 보류 점검이 없습니다." />
      <h4>자문</h4><EvidenceList items={row.advisories} empty="저장된 자문이 없습니다." />
    </section>
    <section className="stack research-detail-section" aria-label="진입·무효화 조건">
      <h3>진입·무효화 조건</h3>
      <p className="muted">{setups[row.price?.strategy ?? ''] ?? row.price?.strategy ?? '가격 셋업 확인 불가'} · {row.price?.session ?? '기준 거래일 확인 불가'}</p>
      <EvidenceList items={[...new Set([...(row.price?.entry_conditions ?? []), ...(row.price?.invalidation_conditions ?? [])])]}
        empty="저장된 진입·무효화 조건이 없습니다." />
    </section>
  </Drawer>
}

const columns = ['종목', '판정', '재무 (70%)', '뉴스 (30%)', '리스크 차감', '총점', '가격 셋업', '보류 점검']
const sortValue: ((row: Evaluation) => string | number | null)[] = [
  row => row.name ?? row.symbol, row => row.action_decision ?? null, row => finite(row.financial_score),
  row => finite(row.news_score), row => finite(row.risk_penalty), row => finite(row.total_score),
  row => setups[row.price?.strategy ?? ''] ?? row.price?.strategy ?? null, row => row.pending_checks?.length ?? null,
]

function Evaluations({ report, symbol, setSymbol }: {
  report: Report; symbol: string | null; setSymbol: (symbol: string | null) => void
}) {
  const [verdict, setVerdict] = useState('')
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<{ column: number; direction: 'ascending' | 'descending' } | null>(null)
  const [page, setPage] = useState(0)
  const rows = useMemo(() => {
    const candidates = new Map(report.candidates?.map(row => [row.symbol, row]))
    return (report.evaluations ?? report.candidates ?? []).map(row => ({ ...row, ...candidates.get(row.symbol) }))
  }, [report])
  const matches = useMemo(() => {
    const query = search.trim().toLocaleLowerCase('ko-KR')
    const filtered = rows.filter(row => (!verdict || row.action_decision === verdict) &&
      (!query || row.symbol.includes(query) || row.name?.toLocaleLowerCase('ko-KR').includes(query)))
    if (!sort) return filtered
    return filtered.sort((a, b) => {
      const left = sortValue[sort.column](a), right = sortValue[sort.column](b)
      if (left === null) return right === null ? 0 : 1
      if (right === null) return -1
      const comparison = typeof left === 'number' && typeof right === 'number' ? left - right
        : String(left).localeCompare(String(right), 'ko-KR')
      return sort.direction === 'ascending' ? comparison : -comparison
    })
  }, [rows, verdict, search, sort])
  const displayedPage = Math.min(page, Math.max(0, Math.ceil(matches.length / pageSize) - 1))
  const active = rows.find(row => row.symbol === symbol)
  return <Panel title="종목별 리서치 판정">
    <div className="research-filters">
      <label className="research-field">종목 검색
        <input type="search" value={search} onChange={event => { setSearch(event.target.value); setPage(0) }} placeholder="종목명 또는 코드" />
      </label>
      <label className="research-field">판정
        <select value={verdict} onChange={event => { setVerdict(event.target.value); setPage(0) }}>
          <option value="">전체 판정</option>
          {Object.entries(verdicts).map(([code, item]) => <option key={code} value={code}>{item.label} ({code})</option>)}
        </select>
      </label>
    </div>
    <p className="muted research-result" role="status">{numeric(matches.length)}종목 / 전체 {numeric(rows.length)}종목</p>
    <Table label="종목별 리서치 판정" columns={columns} sort={{
      column: sort?.column ?? -1, direction: sort?.direction ?? 'ascending', sortableColumns: columns.map((_, index) => index),
      onChange: column => {
        setSort({ column, direction: sort?.column === column
          ? (sort.direction === 'descending' ? 'ascending' : 'descending')
          : (column === 0 || column === 1 || column === 6 ? 'ascending' : 'descending') })
        setPage(0)
      },
    }}>
      {matches.slice(displayedPage * pageSize, (displayedPage + 1) * pageSize).map(row => <tr key={row.symbol}>
        <th scope="row"><button type="button" className="research-symbol" aria-haspopup="dialog" onClick={() => setSymbol(row.symbol)}>
          {row.name && <span>{row.name}</span>}<span className="mono">{row.symbol}</span>
        </button></th>
        <td><Verdict row={row} /></td>
        <td className="mono">{numeric(row.financial_score)}</td><td className="mono">{numeric(row.news_score)}</td>
        <td className="mono">{numeric(row.risk_penalty)}</td><td className="mono">{numeric(row.total_score)}</td>
        <td>{setups[row.price?.strategy ?? ''] ?? row.price?.strategy ?? '확인 불가'}</td>
        <td className="mono">{numeric(row.pending_checks?.length, '건')}</td>
      </tr>)}
      {!matches.length && <tr><td colSpan={columns.length}><Empty>
        {rows.length ? '검색·필터 조건에 맞는 종목이 없습니다.' : '저장된 종목 판정이 없습니다.'}
      </Empty></td></tr>}
    </Table>
    {matches.length > pageSize && <div className="cluster">
      <button type="button" disabled={displayedPage === 0} onClick={() => setPage(displayedPage - 1)}>이전</button>
      <span className="muted mono" aria-live="polite">{displayedPage + 1} / {Math.ceil(matches.length / pageSize)} 페이지</span>
      <button type="button" disabled={(displayedPage + 1) * pageSize >= matches.length} onClick={() => setPage(displayedPage + 1)}>다음</button>
    </div>}
    {active && <ResearchDetails row={active} reportId={report.report_id} close={() => setSymbol(null)} />}
  </Panel>
}

export default function Research() {
  const { query, updateQuery } = useRouteQuery()
  const selection = query.get('report') ?? ''
  const list = useResource<ReportList>(reportsPath)
  const reports = list.data?.reports ?? []
  const latest = list.data?.latest ?? reports[0]
  const id = selection || latest?.report_id
  const detail = useResource<Presence & { report?: Report | null }>(list.data?.present && id
    ? reportsPath + '/' + encodeURIComponent(id) : null)
  const report = detail.data?.present && detail.data.report?.report_id === id ? detail.data.report : null
  useEvidenceMode(evidenceMode(report?.mode))

  return <div className="stack research-page">
    <header className="page-heading"><h1>통합 리서치 &amp; 차트</h1></header>
    <Panel title="통합 리서치 보고서">
      <ResourceState loading={list.loading} error={list.error} />
      {list.data && !list.data.present && <Empty>{researchUnavailable(list.data.reason, '저장된 통합 리서치 보고서가 없습니다.')}</Empty>}
      {list.data?.present && <>
        <label className="research-field">UR 보고서
          <select value={selection} onChange={event => updateQuery({ report: event.target.value, symbol: null })}>
            <option value="">{latest ? `최신 · ${kst(latest.as_of)} · ${latest.report_id}` : '저장된 보고서 없음'}</option>
            {selection && !reports.some(item => item.report_id === selection) && <option value={selection}>{selection}</option>}
            {reports.map(item => <option key={item.report_id} value={item.report_id}>
              {kst(item.as_of)} · {item.report_id}
            </option>)}
          </select>
        </label>
        <ResourceState loading={detail.loading} error={detail.error} />
        {detail.data && !report && <Empty>{researchUnavailable(detail.data.reason, '선택한 리서치 보고서를 찾을 수 없습니다.')}</Empty>}
        {!id && <Empty>저장된 통합 리서치 보고서가 없습니다.</Empty>}
        {report && <p className="cluster muted">
          <span>판정 기준 {kst(report.as_of)}</span>
          <span>가격 기준 거래일 {report.expected_price_session ?? '확인 불가'}</span>
          {report.score_formula && <code>{report.score_formula}</code>}
        </p>}
      </>}
    </Panel>
    {report && <Evaluations key={report.report_id} report={report} symbol={query.get('symbol')}
      setSymbol={symbol => updateQuery({ symbol })} />}
  </div>
}
