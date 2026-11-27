import { useResource } from '../api/client'
import { unavailableReason } from '../api/phaseB'
import type { Presence, ReportList, ReportSummary } from '../api/phaseB'
import { Badge, Empty, Panel, Pnl, ResourceState, Skeleton, SummaryMoney, Table, Select } from '../components/ui'
import { finite, kst, numeric, percent } from '../utils/formatters'
import { evidenceLabel, evidenceText, labelForCode, reportName } from '../utils/labels'
import { useRouteQuery } from '../utils/routeQuery'

const categories: Record<string, string> = {
  CONTRACT: '수주·계약', EARNINGS: '실적', POLICY: '정책', APPROVAL: '인허가', THEME: '사업·테마',
}

type Article = { category: string; title?: string; url?: string; excerpt?: string; published_at?: string }
type Candidate = {
  symbol: string; name?: string; action_decision?: string; expected_holding_period?: string
  turnover?: string | number; turnover_multiple?: string | number; close?: string | number
  return_5d?: string | number; price?: { strategy?: string }
  hermes_duration?: string | null; hermes_evidence_index?: number | null; news_evidence?: Article[]
}
type SwingReport = {
  report_id: string; as_of?: string; price_session?: string; policy_version?: string
  mode?: string; status?: string; candidates?: Candidate[]
}
type PerformanceRecord = {
  report_id: string; symbol: string; trading_days: number; status: string
  gross_return?: string | number | null; net_return?: string | number | null
  entry_date?: string | null; exit_date?: string | null; round_trip_cost_bps?: string | number | null
}
type PerformanceSummary = {
  ruleset_version: string; decision: string; strategy: string; trading_days: number
  sample_count: number; average_net_return: string | number; win_rate: string | number
}
type Performance = Presence & { records?: PerformanceRecord[]; summary?: PerformanceSummary[] }

const statusText: Record<string, string> = {
  COMPLETE: '완료', PENDING_HORIZON: '평가 기한 대기', PENDING_ENTRY: '진입가 확인 대기',
  UNTRADABLE_ENTRY: '진입 불가', ONE_OFF: '단발성 가능', POTENTIALLY_PERSISTENT: '지속 가능', UNCLEAR: '확인 필요',
}

function ratio(value: unknown) {
  const number = finite(value)
  return number === null ? null : number * 100
}

function safeArticleLink(value?: string) {
  if (!value) return null
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

function SwingCandidates({ report, category }: { report: SwingReport; category: string }) {
  const candidates = (report.candidates ?? []).filter(candidate =>
    !category || candidate.news_evidence?.some(article => article.category === category))
  if (!candidates.length) return <Empty>{category ? '이 분류의 촉매가 기록된 후보가 없습니다.' : '저장된 스윙 후보가 없습니다.'}</Empty>
  return <ul className="evidence-list">
    {candidates.map(candidate => {
      const duration = candidate.hermes_duration
      const evidence = candidate.news_evidence ?? []
      return <li key={candidate.symbol} className="stack">
        <header className="section-header">
          <h3>{candidate.name ?? '종목명 확인 불가'} <span className="mono">{candidate.symbol}</span></h3>
          <div className="cluster">
            {candidate.action_decision && <Badge>{labelForCode(candidate.action_decision)}</Badge>}
            <Badge tone={duration === 'POTENTIALLY_PERSISTENT' ? 'ok' : duration === 'ONE_OFF' ? 'warn' : 'unknown'}>
              지속성 {statusText[duration ?? ''] ?? '확인 필요'}
            </Badge>
          </div>
        </header>
        <div className="cluster muted">
          <span>거래대금 <SummaryMoney value={candidate.turnover} /></span>
          <span>20일 대비 {numeric(candidate.turnover_multiple, '배')}</span>
          <span>5거래일 <Pnl value={ratio(candidate.return_5d)} format={percent} /></span>
          {candidate.price?.strategy && <span>{labelForCode(candidate.price.strategy)}</span>}
          {candidate.expected_holding_period && <span>{evidenceText(candidate.expected_holding_period.replace(/(\d+(?:[–~-]\d+)?) trading days?/, '$1거래일'))}</span>}
        </div>
        {evidence.length > 0 && <details open={category ? true : undefined}>
          <summary>촉매 기사 근거 ({evidence.filter(article => !category || article.category === category).length}건)</summary>
          <ul className="evidence-list">
            {evidence.map((article, index) => {
              if (category && article.category !== category) return null
              const href = safeArticleLink(article.url)
              return <li key={index} className="stack">
                <div className="cluster"><Badge>{categories[article.category] ?? '기타 촉매'}</Badge>
                  <span className="muted">{kst(article.published_at)}</span>
                  {candidate.hermes_evidence_index === index && <Badge>지속성 판단 근거</Badge>}
                </div>
                {href ? <a className="action-link" href={href} target="_blank" rel="noreferrer">{article.title ?? '기사 원문'}</a>
                  : <strong>{article.title ?? '기사 제목 확인 불가'}</strong>}
                {article.excerpt && <p>{article.excerpt}</p>}
              </li>
            })}
          </ul>
        </details>}
      </li>
    })}
  </ul>
}

function PerformanceTab({ swingReports }: { swingReports: ReportSummary[] }) {
  const { data: research } = useResource<ReportList>('/api/research/reports')
  const researchReports = research?.reports ?? []
  const knownReports = [...swingReports, ...researchReports]
  function performanceReportName(id: string) {
    const kind = id.startsWith('SW-') ? 'swing' : id.startsWith('UR-') ? 'research' : 'performance'
    return reportName(knownReports.find(report => report.report_id === id) ?? { report_id: id }, kind,
      kind === 'swing' ? swingReports : researchReports)
  }

  const { query, updateQuery } = useRouteQuery()
  const reportId = query.get('performance_report') ?? ''
  const horizon = query.get('horizon') ?? ''
  const all = useResource<Performance>('/api/performance')
  const selected = useResource<Performance>(reportId ? '/api/performance?report_id=' + encodeURIComponent(reportId) : null)
  const resource = reportId ? selected : all
  const reportIds = [...new Set((all.data?.records ?? []).map(row => row.report_id))].sort().reverse()
  const rows = (resource.data?.records ?? []).filter(row => !horizon || String(row.trading_days) === horizon)
  const summaries = (resource.data?.summary ?? []).filter(row => !horizon || String(row.trading_days) === horizon)
  const horizons = [...new Set([5, 20, 60, ...(resource.data?.records ?? []).map(row => row.trading_days)])].sort((a, b) => a - b)
  const statuses = ['COMPLETE', 'PENDING_HORIZON', 'PENDING_ENTRY', 'UNTRADABLE_ENTRY']
  const counts = new Map<string, number>()
  for (const row of rows) counts.set(row.status, (counts.get(row.status) ?? 0) + 1)

  return <div className="stack">
    <Panel title="사후 성과 원장">
      <div className="toolbar">
        <label>보고서
          <Select value={reportId} onChange={event => updateQuery({ performance_report: event.target.value })}>
            <option value="">저장된 전체 보고서</option>
            {reportId && !reportIds.includes(reportId) && <option value={reportId} title={reportId}>{performanceReportName(reportId)}</option>}
            {reportIds.map(id => <option key={id} value={id} title={id}>{performanceReportName(id)}</option>)}
          </Select>
        </label>
        <label>평가 기간
          <Select value={horizon} onChange={event => updateQuery({ horizon: event.target.value })}>
            <option value="">전체 거래일</option>
            {horizons.map(days => <option key={days} value={days}>{days}거래일</option>)}
          </Select>
        </label>
      </div>
      <ResourceState loading={resource.loading} error={resource.error ?? all.error} />
      {resource.data && !resource.data.present && <Empty>{unavailableReason(resource.data.reason, '저장된 사후 성과를 확인할 수 없습니다.')}</Empty>}
      {resource.data?.present && <>
        <p className="muted">세금 별도 항목은 원장에 없습니다. 순수익률은 저장된 왕복 비용을 반영합니다.</p>
        {rows.length > 0
          ? <>
            <div className="cluster" aria-label="평가 상태 분포">
              {statuses.map(status => <Badge key={status} tone={status === 'COMPLETE' ? 'ok' : status === 'UNTRADABLE_ENTRY' ? 'warn' : 'unknown'}>
                {statusText[status]} {counts.get(status) ?? 0}건
              </Badge>)}
            </div>
            <Table label="거래일별 사후 성과" columns={['보고서 / 종목', '기간', '상태', '진입일 → 평가일', '비용 전', '왕복 비용 차감 후', '왕복 비용']}>
              {rows.map(row => <tr key={row.report_id + ':' + row.symbol + ':' + row.trading_days}>
                <th scope="row"><span className="report-name" title={row.report_id}>{performanceReportName(row.report_id)}</span><br />{row.symbol}</th>
                <td>{row.trading_days}거래일</td><td>{statusText[row.status] ?? labelForCode(row.status)}</td>
                <td className="mono">{row.entry_date ?? '—'} → {row.exit_date ?? '—'}</td>
                <td><Pnl value={ratio(row.gross_return)} format={percent} /></td>
                <td><Pnl value={ratio(row.net_return)} format={percent} /></td>
                <td className="mono">{numeric(row.round_trip_cost_bps, 'bps')}</td>
              </tr>)}
            </Table>
          </>
          : <Empty>이 보고서와 기간에 저장된 성과 기록이 없습니다.</Empty>}
      </>}
    </Panel>
    {summaries.length > 0 && <Panel title="완료 표본의 평균과 승률">
      <Table label="사후 성과 집계" columns={['규칙', '판정 / 셋업', '기간', '표본', '평균 순수익률', '승률']}>
        {summaries.map(row => <tr key={JSON.stringify([row.ruleset_version, row.decision, row.strategy, row.trading_days])}>
          <th scope="row">{labelForCode(row.ruleset_version, '저장된 평가 기준')}</th><td>{labelForCode(row.decision)} / {labelForCode(row.strategy)}</td>
          <td>{row.trading_days}거래일</td><td>{row.sample_count}건</td>
          <td><Pnl value={ratio(row.average_net_return)} format={percent} /></td><td>{percent(ratio(row.win_rate))}</td>
        </tr>)}
      </Table>
    </Panel>}
  </div>
}

export default function Swing() {
  const { query, updateQuery } = useRouteQuery()
  const performance = query.get('view') === 'performance'
  const category = query.get('catalyst') ?? ''
  const list = useResource<ReportList>('/api/swing/reports')
  const selected = query.get('report') ?? list.data?.latest?.report_id ?? ''
  const detail = useResource<Presence & { report?: SwingReport }>(!performance && selected
    ? '/api/swing/reports/' + encodeURIComponent(selected) : null)
  const report = detail.data?.report
  const reports = list.data?.reports ?? []

  function tabLink(next: boolean) {
    const params = new URLSearchParams(query)
    if (next) params.set('view', 'performance')
    else params.delete('view')
    const search = params.toString()
    return '/swing' + (search ? '?' + search : '')
  }

  return <div className="stack page-content">
    <header className="page-heading"><h1>테마·스윙 & 성과</h1><p>저장된 스윙 후보와 평가가 완료된 거래일별 성과를 확인합니다.</p></header>
    <nav className="cluster experiment-tabs" aria-label="스윙·성과 보기">
      {[false, true].map(next => <a key={String(next)} href={tabLink(next)} aria-current={next === performance ? 'page' : undefined}
        onClick={event => {
          if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
          event.preventDefault()
          updateQuery({ view: next ? 'performance' : null })
        }}>{next ? '사후 성과 트래커' : '스윙 보고서'}</a>)}
    </nav>
    {performance ? <PerformanceTab swingReports={reports} /> : <Panel title={report ? <span title={report.report_id}>{reportName(report, 'swing', reports)}</span> : '테마·스윙 보고서'}>
      <ResourceState loading={false} error={list.error ?? detail.error} />
      {(list.loading || detail.loading) && <Skeleton label="스윙 보고서 불러오는 중" />}
      {list.data?.present && <div className="toolbar">
        <label>스윙 보고서
          <Select value={selected} onChange={event => updateQuery({ report: event.target.value })}>
            {!selected && <option value="">저장된 보고서 없음</option>}
            {selected && !list.data.reports?.some(row => row.report_id === selected) && <option value={selected} title={selected}>{report ? reportName(report, 'swing', reports) : '선택한 보고서'}</option>}
            {list.data.reports?.map(row => <option key={row.report_id} value={row.report_id} title={row.report_id}>
              {reportName(row, 'swing', reports)}
            </option>)}
          </Select>
        </label>
        <label>촉매 분류
          <Select value={category} onChange={event => updateQuery({ catalyst: event.target.value })}>
            <option value="">전체</option>
            {Object.entries(categories).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </Select>
        </label>
      </div>}
      {list.data && !list.data.present && <Empty>{unavailableReason(list.data.reason, '저장된 스윙 보고서가 없습니다.')}</Empty>}
      {detail.data && !detail.data.present && <Empty>{unavailableReason(detail.data.reason, '선택한 스윙 보고서를 확인할 수 없습니다.')}</Empty>}
      {report && <><div className="summary-line">
        {report.status && report.status !== 'UNKNOWN' && <span>{labelForCode(report.status)}</span>}
        {report.policy_version && <span>{labelForCode(report.policy_version, '스윙 전략')}</span>}
        {evidenceLabel(report.mode) && <span>{evidenceLabel(report.mode)}</span>}
        {report.as_of && <span className="muted">{kst(report.as_of)}</span>}
      </div><SwingCandidates report={report} category={category} /></>}
    </Panel>}
  </div>
}
