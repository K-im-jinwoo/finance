import { useEffect, useMemo, useState } from 'react'
import { postJson, useCapabilities, useEvidenceMode, useRefresh, useResource } from '../api/client'
import { EquityDrawdownChart } from '../components/Charts'
import { Badge, Empty, MetricGrid, Panel, Pnl, ResourceState, Table } from '../components/ui'
import { finite, kst, money, percent } from '../utils/formatters'
import {
  equityPoints, matchingReports, paperPercent, selectedReport,
  type AccountList, type Candidate, type HermesAccount, type HermesReport, type HermesSource, type Quote,
} from './HermesData'

const UNKNOWN = '확인 불가'
const REPORT_PATH = '/api/hermes/reports'
const ACCOUNTS_PATH = '/api/paper-accounts'
const NO_REPORTS: HermesReport[] = []

type Tone = 'ok' | 'warn' | 'danger' | 'unknown'
type Status = readonly [label: string, tone: Tone]

const connectionStatuses: Record<string, Status> = {
  CONNECTED: ['연결됨', 'ok'],
  CONNECTING: ['연결 중', 'warn'],
  DISCONNECTED: ['미연결', 'unknown'],
  MARKET_DATA_UNAVAILABLE: ['시장 시세 확인 불가', 'danger'],
  SOURCE_UNAVAILABLE: ['Hermes 원본 접근 불가', 'danger'],
}
const feedModes: Record<string, Status> = {
  OPERATING_STORE_READ_ONLY: ['운영 시세 저장소 읽기 전용', 'ok'],
  DEDICATED_PUBLIC_REST: ['전용 공개 시세 REST', 'ok'],
  NOT_STARTED: ['수집 미시작', 'unknown'],
}
const authModes: Record<string, Status> = {
  SUPPLIED_ACCESS_TOKEN: ['입력한 Access Token', 'ok'],
  SHARED_CLIENT_TOKEN_CACHE: ['토스 인증 공유 캐시', 'ok'],
  SEPARATE_CLIENT_CREDENTIALS: ['별도 클라이언트 인증', 'ok'],
}
const executionGates: Record<string, Status> = {
  OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED: ['시가 관측·체결 정책 승인 대기', 'warn'],
  DEDICATED_RAW_OPEN_AND_CALENDAR_CONNECTION_REQUIRED: ['원시 시가·정규장 달력 연결 대기', 'warn'],
  VERIFIED_RAW_REGULAR_OPEN_REQUIRED: ['검증된 정규장 시가 대기', 'warn'],
  VERIFIED_OPEN_PROCESSED: ['검증된 시가 처리 완료', 'ok'],
}
const freshnessStatuses: Record<string, Status> = {
  FRESH: ['최근 시세', 'ok'],
  DELAYED: ['시세 지연', 'warn'],
  STALE: ['오래된 시세', 'unknown'],
  MISSING: ['시세 없음', 'unknown'],
  INVALID: ['유효하지 않은 시세', 'danger'],
}
const checkLabels: Record<string, string> = {
  PRICE_LT_SMA20: '가격 · 20일 평균',
  GENERAL_FINANCIAL: '재무 · 영업이익·영업현금흐름',
  DISCLOSURE: '공시',
}
const checkTones: Record<string, Tone> = {
  VALID: 'ok', INVALID: 'danger', UNKNOWN: 'unknown',
  REVIEW_REQUIRED: 'warn', NOT_APPLICABLE: 'unknown',
}
const evidenceReasons: Record<string, string> = {
  'missing/stale/inconsistent price evidence': '가격 근거 누락·지연 또는 기준 불일치',
  'missing/stale/unverified periods': '재무 근거 누락·지연 또는 기간 미확인',
  'specialist thresholds not approved': '업종별 판정 기준 미확정',
  'auto invalidation excluded from v1': '공시 원문 검토 필요',
}

function readQuery() {
  const params = new URLSearchParams(typeof window === 'undefined' ? '' : window.location.search)
  return { account: params.get('account'), report: params.get('report') }
}

function writeQuery(key: 'account' | 'report', value: string | null) {
  if (typeof window !== 'undefined') {
    const url = new URL(window.location.href)
    if (value === null) url.searchParams.delete(key)
    else url.searchParams.set(key, value)
    if (url.href !== window.location.href) window.history.replaceState(window.history.state, '', url)
  }
  return readQuery()
}

function StatusBadge({ value, statuses, showCode = false }: {
  value?: string | null
  statuses: Record<string, Status>
  showCode?: boolean
}) {
  const [label, tone] = (value && statuses[value]) || [value || UNKNOWN, 'unknown']
  return <Badge tone={tone}>{showCode && value && label !== value ? `${value} · ${label}` : label}</Badge>
}

function Connection({ source, account }: { source?: HermesSource, account?: HermesAccount }) {
  const feed = account?.meta.feed
  const gate = feed?.execution_gate ?? account?.meta.execution_gate
  const tokenRejected = feed?.error === 'SUPPLIED_ACCESS_TOKEN_REJECTED_OR_EXPIRED'
  const lastCheckedAt = feed?.polled_at ?? source?.last_success_at
  return (
    <Panel title="Hermes 연결 및 체결 상태">
      <div className="stack">
        <MetricGrid metrics={[
          { label: '연결 상태', value: <StatusBadge value={source?.state} statuses={connectionStatuses} /> },
          { label: '시세 피드', value: <StatusBadge value={feed?.mode ?? source?.mode} statuses={feedModes} /> },
          {
            label: '인증 방식',
            value: tokenRejected
              ? <Badge tone="danger">Access Token 거부 또는 만료</Badge>
              : <StatusBadge value={feed?.auth_mode} statuses={authModes} />,
          },
          { label: '체결 게이트', value: <StatusBadge value={gate} statuses={executionGates} /> },
        ]} />
        <div className="cluster muted">
          <span>최근 자료 확인 {kst(lastCheckedAt)}</span>
          {feed?.poll_seconds != null && <span>자료 확인 {feed.poll_seconds}초 간격</span>}
          {feed?.source_update_seconds != null && <span>원본 시세 수집 {feed.source_update_seconds}초 간격</span>}
          {account?.meta.last_open_session && <span>최근 처리 세션 {account.meta.last_open_session}</span>}
        </div>
        {tokenRejected
          ? <p className="notice">시세 조회가 중단됐습니다. 입력 도구에서 Access Token을 교체해 주세요. 토큰 자동 재발급은 지원되지 않습니다.</p>
          : (feed?.error || source?.market_error || source?.error) &&
            <p className="notice">연결 오류 <span className="mono">{feed?.error ?? source?.market_error ?? source?.error}</span></p>}
      </div>
    </Panel>
  )
}

function AccountControls({ account, onChange }: { account: HermesAccount, onChange: (account: HermesAccount) => void }) {
  const capabilities = useCapabilities()
  const refresh = useRefresh()
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const status = account.state.status
  const writable = capabilities?.read_only === false
  const paused = status === 'PAUSED'
  const controllable = status === 'RUNNING' || paused

  async function control() {
    if (!writable || !controllable || working) return
    setWorking(true)
    setError(null)
    try {
      const path = `${ACCOUNTS_PATH}/${encodeURIComponent(account.account_id)}/${paused ? 'resume' : 'pause'}`
      onChange(await postJson<HermesAccount>(path, {}))
      refresh()
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : '상태 변경에 실패했습니다.')
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="stack">
      <div className="cluster">
        <Badge tone={paused ? 'warn' : status === 'RUNNING' ? 'ok' : 'unknown'}>
          {paused ? '일시중지' : status === 'RUNNING' ? '추천·시세 확인 중' : status}
        </Badge>
        {writable && controllable && <button type="button" onClick={control} disabled={working}>
          {working ? '처리 중…' : paused ? '모의계좌 재개' : '모의계좌 일시중지'}
        </button>}
      </div>
      {writable && controllable && <p className="muted">
        {paused ? '재개 이후 생성된 새 보고서부터 편입을 검토합니다.' : '일시중지하면 대기 거래를 취소하고 시세 평가는 계속합니다.'}
      </p>}
      {error && <p role="alert" className="notice">{error}</p>}
    </div>
  )
}

function AccountOverview({ account }: { account: HermesAccount }) {
  const { state, policy, metrics } = account
  const points = useMemo(() => equityPoints(state.equity, policy.initial_cash), [state.equity, policy.initial_cash])
  const fills = state.trades.map(trade => ({
    time: trade.filled_at,
    label: `${account.securities[trade.symbol]?.name ?? trade.symbol} 가상 ${trade.side === 'BUY' ? '매수' : '매도'}`,
    side: trade.side,
  }))
  return (
    <Panel title="계좌 자산 및 낙폭">
      <div className="stack">
        <MetricGrid metrics={[
          { label: '총자산', value: money(account.current_assets), detail: `현금 ${money(state.cash)}` },
          { label: '계좌 수익률', value: <Pnl value={paperPercent(account.current_return)} format={percent} /> },
          { label: '최대 낙폭 (MDD)', value: <Pnl value={paperPercent(metrics.max_drawdown)} format={percent} /> },
          { label: '보유 종목', value: `${Object.keys(state.positions).length}개`, detail: `대기 ${Object.keys(state.pending).length}건` },
          { label: '가상 체결', value: `${state.trades.length}건`, detail: <>실현손익 <Pnl value={finite(state.realized_pnl)} format={money} /></> },
        ]} />
        <EquityDrawdownChart points={points} fills={fills} label="Hermes 총자산 및 낙폭 추이" />
        <p className="muted">자산 평가 {points.length.toLocaleString('ko-KR')}시점 · 평가 누락 {metrics.valuation_missing_count.toLocaleString('ko-KR')}시점 · 낙폭은 확인된 평가 기록 기준</p>
      </div>
    </Panel>
  )
}

function QuoteStatus({ quote }: { quote?: Quote }) {
  return (
    <div className="cluster">
      <StatusBadge value={quote?.freshness ?? 'MISSING'} statuses={freshnessStatuses} showCode />
      {quote?.age_seconds != null && <span className="muted">경과 {quote.age_seconds.toLocaleString('ko-KR')}초</span>}
    </div>
  )
}

function TopCandidate({ report, quotes }: { report?: HermesReport, quotes?: HermesAccount['watch_quotes'] }) {
  const pick = report?.selected
  const quote = pick ? quotes?.[pick.symbol] : undefined
  return (
    <Panel title="최신 CIO 1순위 추천">
      {!pick ? <Empty>{report ? '명시적인 1순위가 확인되지 않아 자동 편입을 보류합니다.' : '확보한 CIO 최종판정이 없습니다.'}</Empty> :
        <div className="stack">
          <div className="cluster">
            <h3>{pick.name} <span className="mono">{pick.symbol}</span></h3>
            {pick.original_decision && <Badge tone="warn">원본 판정 · {pick.original_decision}</Badge>}
          </div>
          <p className="muted">{report?.report_id ?? report?.key} · {kst(report?.published_at)}</p>
          {pick.reason && <p>{pick.reason}</p>}
          {pick.conditions && <p><strong>추가 확인 조건</strong> {pick.conditions}</p>}
          <div className="cluster">
            <strong>{quote?.freshness === 'FRESH' ? '현재가' : '최근 관측가'} {money(quote?.price)}</strong>
            <QuoteStatus quote={quote} />
          </div>
          <p className="muted">시세 기준 {kst(quote?.source_timestamp)}{quote?.source ? ` · ${quote.source}` : ''}</p>
        </div>}
    </Panel>
  )
}

function Positions({ account }: { account: HermesAccount }) {
  const positions = Object.entries(account.state.positions)
  const pending = Object.entries(account.state.pending)
  return (
    <>
      <Panel title="보유 종목 손익">
        {!positions.length ? <Empty>보유 중인 종목이 없습니다.</Empty> :
          <div className="stack">
            <Table label="Hermes 보유 종목 손익" columns={['종목', '수량', '가상 매수가', '최근 관측가', '평가손익', '비용 반영 수익률', '시세 시각·상태']}>
              {positions.map(([symbol, position]) => {
                const quote = account.position_quotes[symbol]
                return (
                  <tr key={symbol}>
                    <th scope="row">{account.securities[symbol]?.name ?? UNKNOWN} <span className="mono">{symbol}</span></th>
                    <td>{position.quantity.toLocaleString('ko-KR')}주</td>
                    <td>{money(position.entry_price)}</td>
                    <td>{money(quote?.price)}</td>
                    <td><Pnl value={finite(quote?.pnl)} format={money} /></td>
                    <td><Pnl value={paperPercent(quote?.return)} format={percent} /></td>
                    <td><QuoteStatus quote={quote} />{kst(quote?.source_timestamp)}{quote?.source ? ` · ${quote.source}` : ''}</td>
                  </tr>
                )
              })}
            </Table>
            <p className="muted">현재 평가금액에서 매수원금과 매수비용을 차감합니다. 매도비용은 가상 매도 체결 시 반영합니다.</p>
          </div>}
      </Panel>
      <Panel title="편입·매도 대기">
        {!pending.length ? <Empty>대기 중인 가상 거래가 없습니다.</Empty> :
          <div className="stack">
            <p className="muted">
              {account.meta.calendar?.next_regular_open
                ? `다음 확인된 정규장 시가 ${kst(account.meta.calendar.next_regular_open)}`
                : '정규장 달력과 수정되지 않은 실제 시가를 기다리고 있습니다.'}
            </p>
            <div className="workspace-grid">
              {pending.map(([symbol, trade]) => (
                <article className="stack" key={symbol}>
                  <div className="cluster">
                    <Badge tone="warn">{trade.side === 'BUY' ? '편입 대기' : '매도 대기'}</Badge>
                    <strong>{account.securities[symbol]?.name ?? UNKNOWN} <span className="mono">{symbol}</span></strong>
                  </div>
                  <p className="muted">신호 확보 {kst(trade.created_at)} · {trade.report_id ?? UNKNOWN}</p>
                  {trade.causes?.length ? <p>{trade.causes.map(({ code }) => checkLabels[code] ?? code).join(' · ')}</p> : null}
                </article>
              ))}
            </div>
          </div>}
      </Panel>
    </>
  )
}

function ReportCandidate({ candidate, selected }: { candidate: Candidate, selected: boolean }) {
  return (
    <article className="stack">
      <div className="cluster">
        <h3>{candidate.name} <span className="mono">{candidate.symbol}</span></h3>
        <Badge tone={selected ? 'ok' : 'unknown'}>{candidate.rank}순위</Badge>
      </div>
      <p className="muted">{candidate.original_review_tier ?? UNKNOWN} · {candidate.original_decision ?? UNKNOWN}</p>
      {candidate.reason && <p>{candidate.reason}</p>}
      <p><strong>추가 확인 조건</strong> {candidate.conditions ?? UNKNOWN}</p>
      <p><strong>원문 무효화 조건</strong> {candidate.invalidation ?? UNKNOWN}</p>
    </article>
  )
}

function ReportExplorer({ reports, reportKey, onChange }: {
  reports: HermesReport[]
  reportKey: string | null
  onChange: (key: string | null) => void
}) {
  const [search, setSearch] = useState('')
  const visible = useMemo(() => matchingReports(reports, search), [reports, search])
  const chosen = selectedReport(reports, reportKey)
  return (
    <Panel title="CIO 보고서 탐색기">
      <div className="stack">
        <div className="toolbar">
          <label className="stack" htmlFor="hermes-report-search">
            보고서 검색
            <input id="hermes-report-search" type="search" value={search} onChange={event => setSearch(event.target.value)} />
          </label>
          <label className="stack" htmlFor="hermes-report-select">
            보고서 선택
            <select id="hermes-report-select" value={chosen?.key ?? ''} onChange={event => onChange(event.target.value)}>
              <option value="" disabled>보고서를 선택하세요</option>
              {chosen && !visible.includes(chosen) && <option value={chosen.key}>{chosen.report_id ?? chosen.key} · 현재 선택</option>}
              {visible.map(report => <option key={report.key} value={report.key}>
                {report.report_id ?? report.key} · {kst(report.published_at)}
              </option>)}
            </select>
          </label>
          {reportKey !== null && <button type="button" onClick={() => onChange(null)}>최신 보고서 보기</button>}
        </div>
        {search.trim() && <p className="muted" role="status">검색 결과 {visible.length}건</p>}
        {!chosen ? <Empty>{reportKey !== null ? '선택한 원본 보고서를 찾을 수 없습니다.' : '확보한 CIO 보고서가 없습니다.'}</Empty> :
          <>
            <div className="stack">
              <div className="cluster">
                <strong className="mono">{chosen.report_id ?? chosen.key}</strong>
                <Badge tone={chosen.selection_status === 'EXPLICIT_RANK_1' ? 'ok' : 'warn'}>
                  {chosen.selection_status === 'EXPLICIT_RANK_1' ? '명시 순위 확인' : '순위·형식 확인 필요 · 자동 편입 보류'}
                </Badge>
              </div>
              <p className="muted">{chosen.author} · Hermes 생성 {kst(chosen.published_at)} · 최초 확보 {kst(chosen.retrieved_at)}</p>
            </div>
            {chosen.candidates.length ? <div className="workspace-grid">
              {chosen.candidates.map((candidate, index) => <ReportCandidate
                key={`${candidate.rank}-${candidate.symbol}-${index}`}
                candidate={candidate}
                selected={candidate.rank === chosen.selected?.rank && candidate.symbol === chosen.selected?.symbol}
              />)}
            </div> : <Empty>구조화된 후보가 없습니다.</Empty>}
            <details key={chosen.key}>
              <summary>Hermes 최종판정 원문 전체</summary>
              <div className="stack">
                <p className="muted">원본 출처 <span className="mono">{chosen.source}</span></p>
                <p className="muted">SHA-256 <span className="mono">{chosen.content_sha256}</span></p>
                <pre className="report-text">{chosen.content}</pre>
              </div>
            </details>
          </>}
      </div>
    </Panel>
  )
}

function FillsAndChecks({ account }: { account: HermesAccount }) {
  const { trades, checks } = account.state
  const results = Object.entries(checks).flatMap(([symbol, entries]) => entries.map(check => ({ symbol, check })))
  return (
    <>
      <Panel title="가상 체결 원장">
        {!trades.length ? <Empty>가상 체결이 없습니다.</Empty> :
          <Table label="Hermes 가상 체결 원장" columns={['체결 시각', '종목', '구분', '수량', '체결가', '비용', '보고서']}>
            {trades.map(trade => <tr key={trade.trade_id}>
              <td>{kst(trade.filled_at)}</td>
              <th scope="row">{account.securities[trade.symbol]?.name ?? UNKNOWN} <span className="mono">{trade.symbol}</span></th>
              <td>{trade.side === 'BUY' ? '가상 매수' : '가상 매도'}</td>
              <td>{trade.quantity.toLocaleString('ko-KR')}주</td>
              <td>{money(trade.price)}</td>
              <td>{money(trade.fee)}</td>
              <td className="mono">{trade.report_id}</td>
            </tr>)}
          </Table>}
      </Panel>
      <Panel title="무효화 근거 점검">
        {!results.length ? <Empty>점검 기록이 없습니다.</Empty> :
          <Table label="Hermes 무효화 근거" columns={['종목', '점검 항목', '판정', '근거']}>
            {results.map(({ symbol, check }) => <tr key={`${symbol}-${check.code}`}>
              <th scope="row">{account.securities[symbol]?.name ?? UNKNOWN} <span className="mono">{symbol}</span></th>
              <td>{checkLabels[check.code] ?? check.code}</td>
              <td><Badge tone={checkTones[check.status] ?? 'unknown'}>{check.status}</Badge></td>
              <td>
                {check.reason && <div>{evidenceReasons[check.reason] ?? check.reason}</div>}
                {check.evidence_id && <div className="mono">{check.evidence_id}</div>}
                {check.available_at && <div>근거 확보 {kst(check.available_at)}</div>}
                {check.close != null && <div>종가 {money(check.close)} · SMA20 {money(check.sma20)}</div>}
                {!check.reason && !check.evidence_id && !check.available_at && check.close == null && UNKNOWN}
              </td>
            </tr>)}
          </Table>}
      </Panel>
    </>
  )
}

function AccountRules({ account }: { account: HermesAccount }) {
  const { policy } = account
  return (
    <Panel title="계좌 운용 기준">
      <details>
        <summary>정책과 데이터 범위</summary>
        <div className="stack">
          <p>시작 {kst(account.created_at)} · 초기 현금 {money(policy.initial_cash)}</p>
          <p>편입 예산 총자산의 {percent(paperPercent(policy.entry_fraction))}에 매수비용 포함 · 정수 주식 · 최대 {policy.max_positions}종목 · 왕복 {finite(policy.round_trip_bps) ?? UNKNOWN}bps</p>
          <p className="muted">{account.meta.selection} · {policy.policy_status}</p>
          <ul className="evidence-list">
            {[...account.coverage.limitations, ...policy.proposals].map(note => <li key={note}>{note}</li>)}
          </ul>
        </div>
      </details>
    </Panel>
  )
}

export default function Hermes() {
  const [query, setQuery] = useState(readQuery)
  const [controlResult, setControlResult] = useState<HermesAccount>()
  const accounts = useResource<AccountList>(ACCOUNTS_PATH)
  const ids = accounts.data?.accounts
  const accountId = query.account ?? ids?.[0] ?? null
  const knownAccount = accountId !== null && ids?.includes(accountId)
  const resource = useResource<HermesAccount>(knownAccount ? `${ACCOUNTS_PATH}/${encodeURIComponent(accountId)}` : null)
  const fetchedAccount = resource.data?.account_id === accountId ? resource.data : undefined
  const account = knownAccount && controlResult?.account_id === accountId &&
    (!fetchedAccount || Date.parse(controlResult.as_of) >= Date.parse(fetchedAccount.as_of))
    ? controlResult : fetchedAccount
  const archive = useResource<HermesReport[]>(ids && !knownAccount ? REPORT_PATH : null)
  const reports = account?.reports ?? archive.data ?? NO_REPORTS
  useEvidenceMode(account?.coverage.mode)

  useEffect(() => {
    if (query.account === null && accountId) setQuery(writeQuery('account', accountId))
  }, [accountId, query.account])

  useEffect(() => {
    function syncQuery() { setQuery(readQuery()) }
    window.addEventListener('popstate', syncQuery)
    return () => window.removeEventListener('popstate', syncQuery)
  }, [])

  return (
    <div className="stack">
      <header className="section-header">
        <div>
          <h1>Hermes 모의계좌</h1>
          <p className="muted">CIO 최종판정과 비용을 반영한 모의계좌 기록</p>
        </div>
        <label className="stack" htmlFor="hermes-account">
          모의계좌 선택
          <select id="hermes-account" value={accountId ?? ''} disabled={!ids?.length}
            onChange={event => setQuery(writeQuery('account', event.target.value))}>
            {accountId === null && <option value="">모의계좌 없음</option>}
            {accountId !== null && !knownAccount && <option value={accountId} disabled>요청한 계좌</option>}
            {ids?.map(id => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
      </header>
      <ResourceState loading={accounts.loading || resource.loading || archive.loading} error={accounts.error || resource.error || archive.error} />
      <Connection source={accounts.data?.source} account={account} />
      {ids && query.account !== null && !knownAccount &&
        <p className="notice" role="status">요청한 계좌를 찾을 수 없습니다. 계좌 목록에서 다른 계좌를 선택할 수 있습니다.</p>}
      {ids?.length === 0 && query.account === null && <Empty>등록된 Hermes 모의계좌가 없습니다.</Empty>}
      {account && <>
        <AccountControls key={account.account_id} account={account} onChange={setControlResult} />
        <AccountOverview account={account} />
      </>}
      {(account || archive.data) && <TopCandidate report={reports[0]} quotes={account?.watch_quotes} />}
      {account && <Positions account={account} />}
      {(account || archive.data) && <ReportExplorer reports={reports} reportKey={query.report}
        onChange={value => setQuery(writeQuery('report', value))} />}
      {account && <>
        <FillsAndChecks account={account} />
        <AccountRules account={account} />
      </>}
    </div>
  )
}
