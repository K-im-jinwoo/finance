import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, getJson, postJson, useCapabilities, useEvidenceMode, useRefresh, useResource } from '../api/client'
import { unavailableReason } from '../api/phaseB'
import type { Presence } from '../api/phaseB'
import { useRouteQuery } from '../utils/routeQuery'
import { RuntimeSettings } from './Ops'
import { EquityDrawdownChart } from '../components/Charts'
import { Badge, Empty, MetricGrid, Panel, Pnl, ResourceState, Table } from '../components/ui'
import { finite, koreanUnit, kst, money, numeric, percent, spreadText, turnoverText } from '../utils/formatters'
import { equityPoints, labelFor, newest, reasonsFor, RISK_LIMIT, SIGNAL_LIMIT, symbolLabel } from './daytradeData'
import type { Amount, DaytradeData, DaytradeReport, Scan } from './daytradeData'

function Disclosure({ summary, render }: { summary: string; render: () => ReactNode }) {
  const [opened, setOpened] = useState(false)
  return <details open={opened} onToggle={event => setOpened(event.currentTarget.open)}>
    <summary>{summary}</summary>
    {opened && <div className="disclosure-body">{render()}</div>}
  </details>
}

type HistoryItem = { at: string; title: string; detail: string }

function HistoryList({ rows }: { rows: HistoryItem[] }) {
  return <ul className="evidence-list">
    {rows.map(({ at, title, detail }, index) => <li key={at + '-' + index}>
      <strong>{title}</strong><p className="muted">{detail}</p>
    </li>)}
  </ul>
}

function History({ rows, limit, previousLabel, empty }: {
  rows: HistoryItem[]; limit: number; previousLabel: string; empty: string
}) {
  const ordered = newest(rows)
  if (!ordered.length) return <Empty>{empty}</Empty>
  return <>
    <HistoryList rows={ordered.slice(0, limit)} />
    {ordered.length > limit && <Disclosure summary={previousLabel + ' (' + (ordered.length - limit) + '건)'}
      render={() => <HistoryList rows={ordered.slice(limit)} />} />}
  </>
}

function Scans({ scans, names }: { scans: Scan[]; names: Record<string, string> }) {
  const scan = scans.at(-1)
  if (!scan) return <Empty>기록된 탐색 결과가 없습니다.</Empty>
  const prior = scans.slice(0, -1).slice(-10).reverse()
  const rejectedCount = scan.rejected_count ?? scan.rejected.length
  const reasons = new Map<string, number>()
  if (scan.rejected_counts) {
    for (const [code, count] of Object.entries(scan.rejected_counts)) reasons.set(labelFor(code), count)
  } else {
    for (const row of scan.rejected) {
      const reason = labelFor(row.reason || row.reasons?.[0])
      reasons.set(reason, (reasons.get(reason) ?? 0) + 1)
    }
  }
  const rejectedSummary = rejectedCount === scan.rejected.length ? scan.rejected.length + '종목'
    : '상세 ' + scan.rejected.length + ' / 전체 ' + rejectedCount + '종목'
  return <>
    <p><strong>최신 탐색</strong> <span className="mono">{kst(scan.at)}</span> · 기록된 탐색 {scans.length}회</p>
    <p className="muted">선정 {scan.selected.length}종목 · 제외 {rejectedCount}종목</p>
    {scan.selected.length ? <Table label="감시 종목 선정" columns={['종목', '거래대금 / 거래량 순위', '거래대금', '매수·매도 호가차', '선정 사유']}>
      {scan.selected.map((row, index) => <tr key={index}>
        <th scope="row">{symbolLabel(names, row.symbol, row.name)}</th>
        <td className="mono">{row.rank_turnover ?? '—'} / {row.rank_volume ?? '—'}</td>
        <td className="mono">{turnoverText(row.turnover)}</td>
        <td className="mono">{spreadText(row.spread_bps, row.entry_blocked)}</td><td>{reasonsFor(row.selection_reasons)}</td>
      </tr>)}
    </Table> : <Empty>선정된 종목이 없습니다.</Empty>}
    <p className="muted">호가차는 매도 1호가와 매수 1호가의 가격 차이 비율입니다.</p>
    <ul className="cluster reason-list" aria-label="제외 사유 분포">
      {[...reasons].map(([reason, count]) => <li key={reason}><Badge>{reason} {count}종목</Badge></li>)}
    </ul>
    {scan.rejected.length > 0 && <Disclosure summary={'제외 종목 상세 보기 (' + rejectedSummary + ')'}
      render={() => <Table label="감시 종목 제외" columns={['종목', '제외 사유']}>
        {scan.rejected.map((row, index) => <tr key={index}>
          <th scope="row">{symbolLabel(names, row.symbol, row.name)}</th>
          <td>{row.reasons?.length ? reasonsFor(row.reasons) : labelFor(row.reason)}</td>
        </tr>)}
      </Table>} />}
    {prior.length > 0 && <Disclosure summary={'이전 탐색 이력 보기 (최근 ' + prior.length + '건 / 전체 ' + scans.length + '회)'}
      render={() => <Table label="이전 탐색 이력" columns={['탐색 시각', '선정 종목', '제외 수']}>
        {prior.map((item, index) => <tr key={index}><td>{kst(item.at)}</td>
          <td>{item.selected.map(row => symbolLabel(names, row.symbol, row.name)).join(' · ') || '없음'}</td>
          <td>{item.rejected_count ?? item.rejected.length}종목</td></tr>)}
      </Table>} />}
  </>
}

function WonPnl({ amount, compact = false }: { amount: Amount; compact?: boolean }) {
  const format = compact ? koreanUnit : money
  return <Pnl value={finite(amount)} format={format} signedText={format(amount, true)} />
}

function DaytradeMetrics({ report }: { report: DaytradeReport }) {
  const { account, sessions } = report
  const sessionNames = Object.values(sessions).map(labelFor)
  const sessionDates = Object.keys(sessions)
  return <MetricGrid metrics={[
    { label: '초기 자본', value: koreanUnit(account.initial_capital), detail: money(account.initial_capital) + ' · 단타 전용 모의 자본' },
    { label: '최종 현금', value: koreanUnit(account.cash), detail: money(account.cash) + ' · 매수·매도 비용 반영' },
    { label: '최종 평가자산', value: koreanUnit(account.final_equity),
      detail: money(account.final_equity) + (account.valuation_status === 'MARK_UNCERTAIN' ? ' · MARK_UNCERTAIN · 평가 확인 불가' : ' · 매수호가·예상 청산 비용 기준') },
    { label: '총 순손익', value: <WonPnl amount={account.net_pnl} compact />,
      detail: <><WonPnl amount={account.net_pnl} /> · 비용 차감 실현 손익 <WonPnl amount={account.realized_pnl_net} /></> },
    { label: '수익률', value: <Pnl value={finite(account.return_pct)} format={percent} />, detail: '초기 자본 대비 순손익' },
    { label: '완결 거래', value: numeric(account.completed_trades, '회'), detail: '매수부터 전량 매도까지' },
    { label: '승률', value: percent(account.win_rate_pct), detail: '완결 거래만 집계' },
    { label: '관측 최대 낙폭', value: percent(account.mdd_pct),
      detail: account.mdd_uncertain ? '평가 누락으로 MDD 확인 불가' : '기록된 평가 시점 기준' },
    { label: '세션 상태', value: sessionNames.join(' · ') || '관측 중', detail: sessionDates.join(' · ') },
  ]} />
}

function Positions({ report, names }: { report: DaytradeReport; names: Record<string, string> }) {
  const [descending, setDescending] = useState(true)
  const positions = Object.entries(report.positions ?? {})
    .sort((a, b) => ((finite(b[1].cost) ?? 0) - (finite(a[1].cost) ?? 0)) * (descending ? 1 : -1))
  if (!positions.length) return <Empty>저장된 미청산 보유 종목이 없습니다.</Empty>
  return <Table label="미청산 보유 종목" columns={['종목', '수량', '취득원가']}
    sort={{ column: 2, direction: descending ? 'descending' : 'ascending', sortableColumns: [2],
      onChange: () => setDescending(!descending) }}>
    {positions.map(([code, holding]) => <tr key={code}>
      <th scope="row">{symbolLabel(names, code)}</th><td className="mono">{numeric(holding.quantity, '주')}</td>
      <td className="mono">{money(holding.cost)}</td>
    </tr>)}
  </Table>
}

function Trades({ report, names }: { report: DaytradeReport; names: Record<string, string> }) {
  const trades = report.completed_trades ?? []
  if (!trades.length) return <Empty>완결 거래가 없습니다.</Empty>
  return <Table label="완결 거래 원장" columns={['종목', '진입 시각', '청산 시각', '수량', '평균 매수가 / 매도가', '총수수료 / 세금', '실현 순손익']}>
    {trades.map((trade, index) => <tr key={index}>
      <th scope="row">{symbolLabel(names, trade.symbol, trade.name)}</th><td>{kst(trade.entry_at)}</td><td>{kst(trade.exit_at)}</td>
      <td className="mono">{numeric(trade.buy_quantity, '주')}</td>
      <td className="mono">{money(trade.average_buy_price)} / {money(trade.average_sell_price)}</td>
      <td className="mono">{money(trade.fees)} / {money(trade.tax)}</td>
      <td><WonPnl amount={trade.realized_pnl} /></td>
    </tr>)}
  </Table>
}

function Fills({ report, names }: { report: DaytradeReport; names: Record<string, string> }) {
  if (!report.fills.length) return <Empty>모의 체결이 없습니다.</Empty>
  return <Table label="비용 반영 체결" columns={['시각', '종목', '매매', '수량', '평균 체결가', '수수료 / 세금', '실현 순손익', '신호·청산 사유']}>
    {report.fills.map((fill, index) => <tr key={fill.event_id ?? index}>
      <td>{kst(fill.at)}</td><th scope="row">{symbolLabel(names, fill.symbol, fill.name)}</th><td>{labelFor(fill.side)}</td>
      <td className="mono">{numeric(fill.quantity, '주')}</td><td className="mono">{money(fill.price)}</td>
      <td className="mono">{money(fill.fee)} / {money(fill.tax)}</td>
      <td>{fill.side === 'BUY' ? '매도 후 계산' : <WonPnl amount={fill.realized_pnl} />}</td>
      <td>{fill.signal?.exit_reason ? labelFor(fill.signal.exit_reason) : reasonsFor(fill.signal?.reasons)}</td>
    </tr>)}
  </Table>
}

function Signals({ report, names }: { report: DaytradeReport; names: Record<string, string> }) {
  const [action, setAction] = useState('')
  const [query, setQuery] = useState('')
  const signals = report.signals ?? []
  const filtered = signals.filter(signal => {
    const label = symbolLabel(names, signal.symbol, signal.name)
    return (!action || signal.action === action) && label.toLocaleLowerCase('ko-KR').includes(query.trim().toLocaleLowerCase('ko-KR'))
  })
  return <>
    <p className="muted">{['BUY', 'SELL', 'HOLD'].map(value =>
      labelFor(value) + ' ' + signals.filter(signal => signal.action === value).length + '건').join(' · ')}.
      조건 충족률과 수익 확률은 별개입니다.</p>
    <div className="toolbar">
      <label>신호 종류 <select value={action} onChange={event => setAction(event.target.value)}>
        <option value="">전체</option><option value="BUY">매수</option><option value="SELL">매도</option><option value="HOLD">대기</option>
      </select></label>
      <label>종목 검색 <input type="search" value={query} onChange={event => setQuery(event.target.value)} /></label>
    </div>
    <History rows={filtered.map(signal => ({
      at: signal.at,
      title: symbolLabel(names, signal.symbol, signal.name) + ' · ' + labelFor(signal.action) + ' · 조건 충족률 ' + percent(signal.score),
      detail: kst(signal.at) + ' · ' + (signal.exit_reason ? labelFor(signal.exit_reason) : reasonsFor(signal.reasons)),
    }))} limit={SIGNAL_LIMIT} previousLabel="이전 신호 더 보기" empty="조건에 맞는 기록된 신호가 없습니다." />
  </>
}

function Risk({ report, names }: { report: DaytradeReport; names: Record<string, string> }) {
  const risk = report.current_risk
  const rows = [
    ...(report.risk_events ?? []).map(item => ({
      at: item.at, title: labelFor(item.phase), detail: kst(item.at) + ' · ' + (item.locks.length ? reasonsFor(item.locks) : '위험 잠금 없음'),
    })),
    ...(report.quality_events ?? []).map(item => ({
      at: item.at, title: symbolLabel(names, item.symbol, item.name) + ' · 데이터 품질 확인', detail: kst(item.at) + ' · ' + reasonsFor(item.issues),
    })),
    ...(report.decisions ?? []).filter(item => ['SKIPPED', 'REJECTED'].includes(item.status)).map(item => ({
      at: item.at, title: symbolLabel(names, item.symbol, item.name) + ' · 실행 보류', detail: kst(item.at) + ' · ' + labelFor(item.reason),
    })),
  ]
  return <>
    <p><Badge tone={risk?.locks.length ? 'danger' : risk?.entry_allowed ? 'ok' : 'unknown'}>
      {!risk ? '현재 위험 상태 확인 불가'
        : risk.locks.length ? '활성 잠금 · ' + reasonsFor(risk.locks)
          : risk.entry_allowed ? '신규 진입 가능 · 종목별 조건 적용' : labelFor(risk.phase) + ' · 신규 진입 제한'}
    </Badge></p>
    <History rows={rows} limit={RISK_LIMIT} previousLabel="과거 품질·보류 기록 더 보기" empty="기록된 위험·품질 이벤트가 없습니다." />
  </>
}

function TradingReport({ report, markdown }: { report: DaytradeReport; markdown?: string | null }) {
  const names = report.symbol_names ?? {}
  const points = useMemo(() => equityPoints(report.equity_curve ?? [], report.account.initial_capital),
    [report.equity_curve, report.account.initial_capital])
  const fills = (report.fills ?? []).map(fill => ({ time: fill.at, side: fill.side,
    label: symbolLabel(names, fill.symbol, fill.name) + ' · 모의 ' + labelFor(fill.side) + ' ' + numeric(fill.quantity, '주') }))
  const unflattened = Object.values(report.sessions).includes('UNFLATTENED')
  return <>
    {unflattened && <p className="notice notice-warn"><strong>미청산 보유 종목이 남아 있습니다.</strong> 체결 원장에 기록된 청산만 반영합니다.</p>}
    <DaytradeMetrics report={report} />
    <Panel title="장중 평가자산과 위험 구간">
      <EquityDrawdownChart label="당일 평가자산" points={points} fills={fills} />
      {report.account.mdd_uncertain && <p className="muted">평가 누락으로 전체 MDD는 확인할 수 없습니다.
        {report.account.mdd_observed_pct != null && ' 기록된 평가에서 관측한 최대 낙폭 ' + percent(report.account.mdd_observed_pct)}
      </p>}
    </Panel>
    <Panel title="미청산 보유"><Positions report={report} names={names} /></Panel>
    <Panel title="완결 거래"><Trades report={report} names={names} /></Panel>
    <Panel title="감시 종목 탐색"><Scans scans={report.scan_history ?? []} names={names} /></Panel>
    <Panel title="비용 반영 체결"><Fills report={report} names={names} /></Panel>
    <Panel title="매수·매도·대기 신호"><Signals report={report} names={names} /></Panel>
    <Panel title="위험·데이터 품질"><Risk report={report} names={names} /></Panel>
    <Panel title="보고서 원문">
      {markdown ? <Disclosure summary="한국어 마크다운 원문 보기" render={() => <pre className="report-text">{markdown}</pre>} />
        : <Empty>저장된 보고서 원문이 없습니다.</Empty>}
    </Panel>
  </>
}

type SessionResult = { date: string; source?: string; account?: DaytradeReport['account'] }

function DaytradeHistory({ dates }: { dates: string[] }) {
  const recent = dates.slice(0, 5)
  const [results, setResults] = useState<SessionResult[] | null>(null)
  const [loading, setLoading] = useState(false)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])

  async function load() {
    if (pending.current) return
    const controller = new AbortController()
    pending.current = controller
    setLoading(true)
    const rows: SessionResult[] = []
    try {
      for (const day of recent) {
        if (controller.signal.aborted) return
        try {
          const { report } = await getJson<DaytradeData>('/api/daytrade/report?date=' + encodeURIComponent(day), controller.signal)
          rows.push({ date: day, source: report?.data_source ?? report?.dataset_kind, account: report?.account })
        } catch {
          rows.push({ date: day })
        }
      }
      if (!controller.signal.aborted) setResults(rows)
    } finally {
      pending.current = null
      if (!controller.signal.aborted) setLoading(false)
    }
  }

  const realized = results?.flatMap(row => {
    const value = finite(row.account?.realized_pnl_net)
    return value === null ? [] : [value]
  }) ?? []
  const sameSource = results?.[0]?.source != null && results.every(row => row.source === results[0].source)
  const complete = results && realized.length === results.length && sameSource
  return <Panel title="일자별 누적 성과" actions={
    <button onClick={() => void load()} disabled={loading}>
      {loading ? '원장 집계 중…' : '최근 ' + recent.length + '개 원장 집계'}
    </button>
  }>
    {results && <>
      <p role="status">{results.length}개 중 {realized.length}개 원장에서 실현손익을 확인했습니다.
        {complete && <> 누적 실현손익 <Pnl value={realized.reduce((sum, value) => sum + value, 0)} format={money} /></>}
      </p>
      <Table label="일자별 단타 성과" columns={['거래일', '데이터 출처', '완결 거래', '순실현손익', '평가 손익']}>
        {results.map(row => <tr key={row.date}>
          <th scope="row" className="mono">{row.date}</th>
          <td>{row.source ?? '원장 확인 불가'}</td>
          <td>{numeric(row.account?.completed_trades, '건')}</td>
          <td><Pnl value={finite(row.account?.realized_pnl_net)} format={money} /></td>
          <td><Pnl value={finite(row.account?.net_pnl)} format={money} /></td>
        </tr>)}
      </Table>
    </>}
  </Panel>
}

type Sessions = Presence & { sessions?: string[]; latest?: string | null }

function validDate(value: string) {
  const timestamp = Date.parse(value)
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(timestamp) &&
    new Date(timestamp).toISOString().slice(0, 10) === value
}

export default function Daytrade() {
  const capability = useCapabilities()
  const refresh = useRefresh()
  const { query, updateQuery } = useRouteQuery()
  const date = query.get('date')
  const sessionsSupported = capability?.daytrade_sessions_available === true
  const sessions = useResource<Sessions>(sessionsSupported ? '/api/daytrade/sessions' : null)
  const dates = sessions.data?.sessions ?? []
  const validSelection = date === null || validDate(date)
  const reportUrl = date === null ? '/api/daytrade/report' : sessionsSupported && validSelection
    ? '/api/daytrade/report?date=' + encodeURIComponent(date) : null
  const { data, loading, error } = useResource<DaytradeData>(reportUrl)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const report = data?.report
  useEvidenceMode(report?.data_source ?? report?.dataset_kind)

  async function startSample() {
    if (date !== null || busy || capability?.read_only !== false || !data?.sample_available || data.present) return
    setBusy(true)
    setActionError(null)
    try {
      await postJson<DaytradeData>('/api/daytrade/simulate-sample', {})
      refresh()
    } catch (failure) {
      setActionError(failure instanceof Error ? failure.message : '샘플 실행 실패')
      if (failure instanceof ApiError && failure.status === 409) refresh()
    } finally {
      setBusy(false)
    }
  }

  return <div className="stack page-content">
    <header className="page-heading">
      <div><h1>당일 단타</h1><p>KRX+NXT 통합 시세 · 비용 반영 모의계좌</p></div>
      <div className="toolbar">
        {sessionsSupported && (dates.length > 0 || date !== null) && <label>거래일
          <select value={date ?? ''} onChange={event => updateQuery({ date: event.target.value })}>
            <option value="">기본 원장</option>
            {date !== null && !dates.includes(date) && <option value={date}>현재 선택 · {date || '잘못된 날짜'}</option>}
            {dates.map(day => <option key={day}>{day}</option>)}
          </select>
        </label>}
        {date === null && capability?.read_only === false && data?.sample_available && !data.present &&
          <button className="primary-button" onClick={() => void startSample()} disabled={busy}>
            {busy ? '샘플 계산 중…' : '오프라인 샘플 실행'}
          </button>}
      </div>
    </header>
    <ResourceState loading={loading || sessions.loading} error={error ?? sessions.error} />
    {date !== null && !validSelection && <p className="notice notice-warn">거래일은 YYYY-MM-DD 형식의 유효한 날짜여야 합니다.</p>}
    {date !== null && capability && !sessionsSupported && <p className="notice">이 서버는 날짜별 원장 조회를 제공하지 않습니다.</p>}
    {actionError && <p role="status" className="notice notice-danger">{actionError}</p>}
    {data && !report && <p className="notice">{unavailableReason(data.reason, date
      ? '선택한 거래일에 저장된 단타 원장이 없습니다.' : '저장된 단타 원장이 없습니다.')}
      {date === null && capability?.read_only === false && data.sample_available &&
        ' 오프라인 샘플을 실행하면 모의 결과를 확인할 수 있습니다.'}</p>}
    {dates.length > 1 && <DaytradeHistory key={dates.slice(0, 5).join(',')} dates={dates} />}
    {report && <>
      <div className="status-band stack" aria-label="단타 데이터 출처와 비용">
        <div className="cluster"><Badge>{report.profile || 'UNKNOWN'}</Badge>
          <Badge>{report.data_source ?? report.dataset_kind}</Badge>
          <Badge tone={report.paper_ready ? 'ok' : 'warn'}>PAPER_READY={String(report.paper_ready)}</Badge>
          <span className="mono">{report.mode}</span>
        </div>
        <p>{report.cost_policy_status ?? '비용 정책 미확인'} · {report.cost_policy_notice ?? '비용 기준 확인 불가'}</p>
      </div>
      {data?.live_settings && <Panel title="세션 설정"><RuntimeSettings value={data.live_settings} /></Panel>}
      <TradingReport key={date ?? 'default'} report={report} markdown={data?.markdown} />
    </>}
  </div>
}
