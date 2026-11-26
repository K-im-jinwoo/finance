import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, MouseEvent } from 'react'
import { evidenceMode, postJson, useCapabilities, useEvidenceMode, useRefresh, useResource } from '../api/client'
import { CostComparisonChart, EquityDrawdownChart } from '../components/Charts'
import { Badge, Drawer, Empty, MetricGrid, Panel, Pnl, ResourceState, Table } from '../components/ui'
import { kst, money } from '../utils/formatters'
import {
  comparisonCosts, decimal, equityPoints, experimentQuery, hasExperimentEvidence, observedReentrySignals,
  percentagePoints, resolveExperimentEvidence,
} from './experimentModel'
import type {
  AlertDraft, DetailQuery, Experiment, ExperimentCost, ExperimentSummary,
  Observation, VerificationStatus,
} from './experimentModel'

const labels: Record<string, string> = {
  READY: '실행 대기',
  RUNNING: '계산 중',
  PAUSED: '일시중지 · 평가는 계속',
  COMPLETED: '완료',
  FAILED: '실패',
  BUY: '가상 매수',
  SELL: '가상 매도',
  INVALIDATION: '무효화 확인',
  EVENING_SUMMARY: '거래일 저녁 요약',
  ERROR: '오류',
  SYNTHETIC: '합성 자료',
  OBSERVED: '관측 자료 검증',
}
const actionStates = { start: 'READY', pause: 'RUNNING', resume: 'PAUSED' } as const
const actionLabels = { start: '실험 시작', pause: '일시중지', resume: '재개' } as const
type ControlAction = keyof typeof actionStates
type DetailSelection = Partial<Record<DetailQuery, string>>
type ExperimentLink = {
  href: string
  onClick: (event: MouseEvent<HTMLAnchorElement>) => void
}
type DetailLink = (selection: DetailSelection, experimentId?: string) => ExperimentLink

const experimentIdPattern = /^E-[a-f0-9]{24}$/
const basePath = '/api/experiments'
const missingExperiment = '선택한 실험을 찾을 수 없습니다. 과거 링크를 최신 실험으로 대체하지 않습니다.'
const missingDetail = '해당 실험에 상세 기록이 없습니다. 최신 보고서로 대체하지 않습니다.'
const kstDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul' })
const label = (value: string) => labels[value] ?? value
const experimentOption = (item: ExperimentSummary) => (
  <option key={item.experiment_id} value={item.experiment_id}>
    {item.experiment_id.slice(0, 12)} · {item.cost_bps}bps · {label(item.state)}
  </option>
)
const count = (value?: number) => value == null ? '확인 불가' : value.toLocaleString('ko-KR')
const percent = (value: number | null) => value === null ? '확인 불가' : value.toFixed(2) + '%'

function StatusBadge({ status }: { status: string }) {
  const tone = status === 'COMPLETED' || status === 'RUNNING' ? 'ok'
    : status === 'PAUSED' ? 'warn' : status === 'FAILED' ? 'danger' : 'unknown'
  return <Badge tone={tone}>{label(status)}</Badge>
}

function useExperimentQuery() {
  const [params, setParams] = useState(() => new URLSearchParams(window.location.search))
  useEffect(() => {
    const update = () => setParams(new URLSearchParams(window.location.search))
    window.addEventListener('popstate', update)
    return () => window.removeEventListener('popstate', update)
  }, [])

  const write = useCallback((next: URLSearchParams) => {
    const search = next.toString()
    window.history.replaceState(window.history.state, '', '/experiments' + (search ? '?' + search : '') + window.location.hash)
    setParams(next)
  }, [])

  function link(next: URLSearchParams): ExperimentLink {
    return {
      href: '/experiments?' + next.toString(),
      onClick(event) {
        if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
        event.preventDefault()
        write(next)
      },
    }
  }

  return { params, write, link }
}

export default function Experiments() {
  const { params, write, link } = useExperimentQuery()
  const list = useResource<ExperimentSummary[]>(basePath)
  const capabilities = useCapabilities()
  const refresh = useRefresh()
  const [cost, setCost] = useState<ExperimentCost>(30)
  const [pending, setPending] = useState<'create' | ControlAction | null>(null)
  const [actionResult, setActionResult] = useState<Experiment | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const actionLocked = useRef(false)
  const explicitlySelected = params.get('experiment')
  const defaultId = (list.data?.find(item => item.cost_bps === '30') ?? list.data?.[0])?.experiment_id
  const selected = explicitlySelected ?? defaultId
  const detailOpen = hasExperimentEvidence(params)
  const comparing = params.get('view') === 'costs' && !detailOpen
  const malformedSelection = explicitlySelected !== null && !experimentIdPattern.test(explicitlySelected)
  const selectedPath = selected && !malformedSelection ? basePath + '/' + selected : null
  const experiment = useResource<Experiment>(comparing ? null : selectedPath)
  const fetched = experiment.data?.experiment_id === selected ? experiment.data : undefined
  const applied = actionResult?.experiment_id === selected ? actionResult : null
  // Reflect accepted control while a polling read may still carry the previous status.
  const awaitingActionRead = applied !== null && !experiment.error && (
    !fetched || fetched.cursor < applied.cursor ||
    (fetched.cursor === applied.cursor && fetched.state.status !== applied.state.status)
  )
  const result = awaitingActionRead ? applied : fetched
  const selectedSummary = list.data?.find(item => item.experiment_id === selected)
  const canWrite = capabilities?.read_only === false
  useEvidenceMode(evidenceMode(result?.coverage.mode ?? selectedSummary?.mode))
  useEffect(() => {
    if (defaultId && explicitlySelected === null) {
      write(experimentQuery(params, { experiment: defaultId }))
    }
  }, [defaultId, explicitlySelected, params, write])
  useEffect(() => {
    if (!awaitingActionRead) setActionResult(null)
  }, [awaitingActionRead])
  const detailLink: DetailLink = (selection, experimentId = selected) => link(experimentQuery(params, {
    experiment: experimentId ?? null,
    view: null,
    ...selection,
  }, true))

  function selectExperiment(id: string) {
    setActionError(null)
    write(experimentQuery(params, { experiment: id }, true))
  }

  async function runAction(action: 'create' | ControlAction) {
    if (actionLocked.current || !canWrite) return
    if (action !== 'create' && (!selectedPath || result?.coverage.mode !== 'SYNTHETIC' ||
      result.state.status !== actionStates[action])) return
    actionLocked.current = true
    setPending(action)
    setActionError(null)
    try {
      if (action === 'create') {
        const created = await postJson<{ experiment_id: string }>(basePath, { cost_bps: String(cost) })
        if (window.location.pathname !== '/experiments') return
        write(experimentQuery(new URLSearchParams(window.location.search), {
          experiment: created.experiment_id,
          view: null,
        }, true))
      } else {
        const updated = await postJson<Experiment>(selectedPath + '/' + action, {})
        setActionResult(updated)
      }
      refresh()
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '실험 요청에 실패했습니다.')
    } finally {
      actionLocked.current = false
      setPending(null)
    }
  }

  function create(event: FormEvent) {
    event.preventDefault()
    void runAction('create')
  }

  const missingSelection = malformedSelection || experiment.error === '요청한 기록을 찾을 수 없습니다.'
  const experimentError = missingSelection ? missingExperiment : experiment.error
  return (
    <div className="stack">
      <header className="section-header">
        <div>
          <h1>계산 검증 실험</h1>
          <p className="muted">왕복 비용은 실험 가정입니다. 실제 3년 성과 검증은 자료 확보 후 진행합니다.</p>
        </div>
      </header>
      <div className="toolbar">
        <label className="cluster">
          실험
          <select value={selected ?? ''} onChange={event => selectExperiment(event.target.value)} disabled={pending !== null}>
            {!selectedSummary && <option value={selected ?? ''} disabled>{selected ? selected.slice(0, 12) : '실험 선택'}</option>}
            {list.data?.map(experimentOption)}
          </select>
        </label>
        {canWrite && <form className="cluster" onSubmit={create}>
          <label className="cluster">
            새 실험 왕복 비용
            <select value={cost} onChange={event => setCost(Number(event.target.value) as ExperimentCost)} disabled={pending !== null}>
              <option value={30}>30bps · 기본 가정</option>
              <option value={10}>10bps · 민감도 가정</option>
              <option value={60}>60bps · 민감도 가정</option>
            </select>
          </label>
          <button type="submit" disabled={pending !== null}>
            {pending === 'create' ? '실험 생성 중…' : '별도 검증 실험 생성'}
          </button>
        </form>}
      </div>
      <ResourceState loading={list.loading} error={list.error} />
      {actionError && <p role="alert" className="notice">{actionError}</p>}
      <nav className="cluster" aria-label="검증 실험 보기">
        <a {...link(experimentQuery(params, { view: null }, true))} aria-current={!comparing ? 'page' : undefined}>단일 실험</a>
        <a {...link(experimentQuery(params, { view: 'costs' }, true))} aria-current={comparing ? 'page' : undefined}>비용 민감도 비교</a>
      </nav>

      {comparing ? (
        <>
          {(malformedSelection || (!list.loading && selected && !selectedSummary && !list.error)) &&
            <ResourceState loading={false} error={missingExperiment} />}
          <CostComparison
            options={list.data ?? []}
            base={selectedSummary}
            params={params}
            onSelect={(bps, id) => write(experimentQuery(params, { ['cost' + bps]: id }))}
          />
        </>
      ) : (
        <>
          <ResourceState loading={experiment.loading} error={experimentError} />
          {!selected && !list.loading && !list.error && !malformedSelection && <Empty>생성된 검증 실험이 없습니다.</Empty>}
          {result && <SingleExperiment
            key={result.experiment_id}
            result={result}
            detailLink={detailLink}
            canWrite={canWrite}
            pending={pending}
            onAction={runAction}
          />}
        </>
      )}
      <OracleVerification />
      {detailOpen && <EvidenceDrawer
        params={params}
        result={result}
        loading={list.loading || experiment.loading}
        error={malformedSelection ? null : experiment.error ?? list.error}
        onClose={() => write(experimentQuery(params, {}, true))}
      />}
    </div>
  )
}

function CostComparison({ options, base, params, onSelect }: {
  options: ExperimentSummary[]
  base?: ExperimentSummary
  params: URLSearchParams
  onSelect: (bps: ExperimentCost, id: string) => void
}) {
  const comparable = base ? options.filter(item => item.dataset_id === base.dataset_id && item.mode === base.mode) : []
  const slots = comparisonCosts.map(bps => {
    const matches = comparable.filter(item => item.cost_bps === String(bps))
    const defaultId = base?.cost_bps === String(bps) ? base.experiment_id : matches[0]?.experiment_id
    const selectedId = params.get('cost' + bps) ?? defaultId ?? ''
    const selected = matches.find(item => item.experiment_id === selectedId)
    return { bps, matches, selectedId, selected }
  })
  const results = [
    useResource<Experiment>(slots[0].selected ? basePath + '/' + slots[0].selectedId : null),
    useResource<Experiment>(slots[1].selected ? basePath + '/' + slots[1].selectedId : null),
    useResource<Experiment>(slots[2].selected ? basePath + '/' + slots[2].selectedId : null),
  ]
  const rows = slots.map((slot, index) => ({
    ...slot,
    result: results[index].data?.experiment_id === slot.selectedId ? results[index].data : undefined,
    loading: results[index].loading,
    error: results[index].error,
  }))
  const series = rows.flatMap(({ bps, result }) => result ? [{
    bps,
    points: result.state.equity.map(row => ({ time: row.at, equity: decimal(row.total_assets) })),
  }] : [])
  const selectedCursors = rows.flatMap(({ result }) => result ? [result.cursor] : [])

  return <Panel title="왕복 거래비용 비교">
    <div className="stack">
      {base && <p className="muted">선택한 실험과 같은 데이터셋에서 비교합니다. <Badge>{base.mode}</Badge></p>}
      <div className="cluster">
        {slots.map(({ bps, matches, selectedId, selected }) => <label className="cluster" key={bps}>
          <span className="mono">{bps}bps</span>
          <select value={selectedId} onChange={event => onSelect(bps, event.target.value)} aria-label={bps + 'bps 비교 실험'}>
            <option value="">{matches.length ? '비교 제외' : '같은 데이터셋의 실험 없음'}</option>
            {!selected && selectedId && <option value={selectedId} disabled>현재 데이터셋에서 찾을 수 없음</option>}
            {matches.map(experimentOption)}
          </select>
        </label>)}
      </div>
      {new Set(selectedCursors).size > 1 && <p className="notice">실험별 진행 시점이 다릅니다. 표에 각 실험의 최신 시점과 진행 상태를 표시합니다.</p>}
      {series.some(({ points }) => points.length > 0)
        ? <CostComparisonChart series={series} />
        : <Empty>비교할 평가 시점이 없습니다.</Empty>}
      <Table label="비용별 핵심 지표" columns={['왕복 비용', '진행 상태', '총자산', '수익률', '총비용', '실현손익', 'MDD', '승률']}>
        {rows.map(({ bps, result, loading, error }) => {
          const last = result?.state.equity.at(-1)
          return <tr key={bps}>
            <th scope="row" className="mono">{bps}bps</th>
            <td>{result
              ? <><StatusBadge status={result.state.status} /><br /><span className="mono">{result.cursor}/{result.total_events}</span> · {kst(result.state.clock)}
                {error && <p className="muted" role="status">새로고침 실패 · 마지막 자료</p>}</>
              : error ?? (loading ? '불러오는 중…' : '확인 불가')}</td>
            <td className="mono">{money(decimal(last?.total_assets))}</td>
            <td><Pnl value={percentagePoints(last?.return)} format={percent} /></td>
            <td className="mono">{money(decimal(result?.metrics.total_cost))}</td>
            <td><Pnl value={decimal(result?.metrics.realized_pnl)} format={money} /></td>
            <td><Pnl value={percentagePoints(result?.metrics.max_drawdown)} format={percent} /></td>
            <td className="mono">{percent(percentagePoints(result?.metrics.win_rate))}</td>
          </tr>
        })}
      </Table>
    </div>
  </Panel>
}

function SingleExperiment({ result, detailLink, canWrite, pending, onAction }: {
  result: Experiment
  detailLink: DetailLink
  canWrite: boolean
  pending: 'create' | ControlAction | null
  onAction: (action: ControlAction) => void
}) {
  const { state, metrics, coverage, policy } = result
  const last = state.equity.at(-1)
  const currentDay = state.clock ? kstDay.format(new Date(state.clock)) : null
  const changes = state.outbox.filter(item => currentDay && kstDay.format(new Date(item.at)) === currentDay).reverse()
  const points = equityPoints(state.equity, policy.initial_cash)
  const fills = state.trades.map(trade => ({
    time: trade.filled_at,
    side: trade.side,
    label: trade.symbol + ' · ' + label(trade.side) + ' ' + trade.quantity + '주',
  }))
  const drafts = useResource<AlertDraft[]>(basePath + '/' + result.experiment_id + '/drafts')

  return <div className="stack">
    <div className="cluster">
      <StatusBadge status={state.status} />
      <Badge>{coverage.mode}</Badge>
      <span className="muted">{label(coverage.mode)} · 왕복 {policy.round_trip_bps}bps</span>
      <span className="mono">{result.cursor}/{result.total_events} 시점</span>
      <span>기준 {kst(state.clock)}</span>
    </div>
    <MetricGrid metrics={[
      { label: '총자산', value: money(decimal(last?.total_assets)), detail: '현금 ' + money(decimal(state.cash)) },
      { label: '계좌 수익률', value: <Pnl value={percentagePoints(last?.return)} format={percent} /> },
      { label: '최대낙폭', value: <Pnl value={percentagePoints(metrics.max_drawdown)} format={percent} />, detail: '평가 관측시점 기준' },
      { label: '가상 체결', value: state.trades.length + '건', detail: '완료 보유 ' + metrics.completed_round_trips + ' · 열린 보유 ' + metrics.open_positions },
    ]} />
    <Panel title="자산 추이와 낙폭" actions={canWrite && coverage.mode === 'SYNTHETIC' ? (
      <div className="cluster">
        {(Object.keys(actionStates) as ControlAction[]).map(action => <button
          key={action}
          type="button"
          disabled={state.status !== actionStates[action] || pending !== null}
          onClick={() => onAction(action)}
        >{pending === action ? actionLabels[action] + ' 처리 중…' : actionLabels[action]}</button>)}
      </div>
    ) : undefined}>
      <div className="stack">
        {points.length
          ? <EquityDrawdownChart points={points} fills={fills} label={label(coverage.mode) + ' 모의계좌 자산과 낙폭'} />
          : <Empty>평가 자료가 확보되면 자산 추이를 표시합니다.</Empty>}
        {metrics.valuation_missing_count > 0 && <p className="notice">평가가 누락된 {metrics.valuation_missing_count}개 시점은 차트에서 공백으로 표시합니다.</p>}
        <div className="cluster">
          <span>기본 자금 <b className="mono">{money(decimal(policy.initial_cash))}</b></span>
          <span>총비용 <b className="mono">{money(decimal(metrics.total_cost))}</b></span>
          <span>실현손익 <Pnl value={decimal(metrics.realized_pnl)} format={money} /></span>
          <span>현금 비중 <b className="mono">{percent(percentagePoints(metrics.cash_ratio))}</b></span>
          <span>완료 구간 승률 <b className="mono">{percent(percentagePoints(metrics.win_rate))}</b></span>
          <span className="muted">벤치마크 미선정</span>
        </div>
      </div>
    </Panel>
    <Panel title="기준일의 변화">
      {changes.length ? <ul className="evidence-list">
        {changes.map(item => <li key={item.event_id}>
          <a {...detailLink({ event: item.event_id })} className="cluster">
            <span>{label(item.kind)}</span>
            <strong className="mono">{item.symbol || '계좌'}</strong>
            <span className="muted">{kst(item.at)}</span>
          </a>
        </li>)}
      </ul> : <Empty>기준일에 새 변화가 없습니다.</Empty>}
    </Panel>
    <PositionChecks state={state} />
    <Panel title="가상 체결 원장">
      {state.trades.length ? <Table label="가상 체결 원장" columns={['체결시각 KST', '종목', '구분', '수량', '가격', '비용', '상세']}>
        {state.trades.map(trade => <tr key={trade.trade_id}>
          <td className="mono">{kst(trade.filled_at)}</td>
          <th scope="row" className="mono">{trade.symbol}</th>
          <td>{label(trade.side)}</td>
          <td className="mono">{trade.quantity}주</td>
          <td className="mono">{money(decimal(trade.price))}</td>
          <td className="mono">{money(decimal(trade.fee))}</td>
          <td><a {...detailLink({ trade: trade.trade_id, report: trade.report_id })} aria-label={trade.symbol + ' ' + label(trade.side) + ' 거래 근거'}>거래 근거</a></td>
        </tr>)}
      </Table> : <Empty>아직 가상 체결이 없습니다.</Empty>}
    </Panel>
    <Observations state={state} detailLink={detailLink} />
    <Panel title="원본 판정과 추천 이력">
      {state.reports.length ? <ul className="evidence-list">
        {state.reports.slice().reverse().map(report => <li key={report.report_id}>
          <a {...detailLink({ report: report.report_id })} className="stack">
            <strong className="mono">{kst(report.at)}</strong>
            <span className="muted">{report.candidates.map(candidate => candidate.symbol + ' · ' + candidate.decision + ' / ' + candidate.review_tier).join(' | ')}</span>
          </a>
        </li>)}
      </ul> : <Empty>보고서가 없습니다.</Empty>}
    </Panel>
    <Panel title="Telegram 알림 초안">
      <div className="stack">
        <p className="muted">발송 비활성 · 공개 주소 미설정</p>
        <ResourceState loading={drafts.loading} error={drafts.error} />
        {drafts.data?.length ? <ul className="evidence-list">
          {drafts.data.slice().reverse().map(draft => <li key={draft.event_id}>
            <DraftItem draft={draft} detailLink={detailLink} />
          </li>)}
        </ul> : !drafts.loading && !drafts.error ? <Empty>발송 초안이 없습니다.</Empty> : null}
      </div>
    </Panel>
    <Panel title="데이터 범위와 실험 설정">
      <details>
        <summary>데이터셋 · 적용 정책 · 원장 지표 · 체결 대기</summary>
        <pre className="report-text">{JSON.stringify({ coverage, policy, metrics, pending: state.pending }, null, 2)}</pre>
      </details>
    </Panel>
  </div>
}

function PositionChecks({ state }: { state: Experiment['state'] }) {
  const positions = Object.entries(state.positions)
  const checkedSymbols = Object.entries(state.checks)
  const invalidations = Object.entries(state.invalidated)
  return <Panel title="보유와 무효화 점검">
    <div className="stack">
      {positions.length ? <Table label="보유 종목" columns={['종목', '수량', '진입가', '원본 판정', '무효화 점검']}>
        {positions.map(([symbol, position]) => <tr key={symbol}>
          <th scope="row" className="mono">{symbol}</th>
          <td className="mono">{position.quantity}주</td>
          <td className="mono">{money(decimal(position.entry_price))}</td>
          <td>{position.original_decision}</td>
          <td><div className="cluster">
            {state.checks[symbol]?.length
              ? state.checks[symbol].map(check => <CheckBadge key={check.code} check={check} />)
              : <Badge>UNKNOWN · 확인 불가</Badge>}
          </div></td>
        </tr>)}
      </Table> : <Empty>보유 종목이 없습니다.</Empty>}
      {checkedSymbols.length > 0 && <details>
        <summary>종목별 최신 점검 근거 ({checkedSymbols.length}종목)</summary>
        <Table label="최신 무효화 점검 근거" columns={['종목', '점검', '사용 가능 시각 KST', '근거']}>
          {checkedSymbols.flatMap(([symbol, checks]) => checks.map(check => <tr key={symbol + check.code}>
            <th scope="row" className="mono">{symbol}</th>
            <td><CheckBadge check={check} /></td>
            <td>{kst(check.available_at)}</td>
            <td>{check.evidence_id ?? check.reason ?? '확인 불가'}</td>
          </tr>))}
        </Table>
      </details>}
      {invalidations.length > 0 && <details>
        <summary>원장에 남은 무효화 기록 ({invalidations.length}종목)</summary>
        <Table label="원장 무효화 기록" columns={['종목', '무효화 시각 KST', '기록된 원인', '근거']}>
          {invalidations.flatMap(([symbol, record]) => record.causes.map(check => <tr key={symbol + check.code}>
            <th scope="row" className="mono">{symbol}</th>
            <td>{kst(record.at)}</td>
            <td><CheckBadge check={check} /></td>
            <td>{check.evidence_id ?? check.reason ?? '확인 불가'}</td>
          </tr>))}
        </Table>
      </details>}
      <p className="muted">공시는 검토 대상으로 기록합니다. 자동 무효화는 미적용입니다.</p>
    </div>
  </Panel>
}

function CheckBadge({ check }: { check: Experiment['state']['checks'][string][number] }) {
  const tone = check.status === 'VALID' ? 'ok' : check.status === 'INVALID' ? 'danger'
    : check.status === 'REVIEW_REQUIRED' ? 'warn' : 'unknown'
  return <Badge tone={tone}>{check.code} · {check.status}</Badge>
}

const observationLabels: Record<string, string> = {
  REVIEW: '검토',
  MAINTAIN: '보유 유지',
  NEW: '신규 대기',
  PENDING: '대기',
  PENDING_MAINTAIN: '대기 유지',
  DEFERRED: '보류',
  DEFER_OLD_SIGNAL: '재개 전 신호 보류',
  DEFER_UNRESOLVED_INVALIDATION: '무효화 미해소 · 재진입 보류',
  CANCELLED: '취소',
  USER_PAUSE: '사용자 일시중지',
  FAILED: '실험 실패',
  END_OF_PERIOD: '검증 기간 종료',
  OPEN_UNAVAILABLE_OR_SUSPENDED: '시가 미확보 또는 거래정지',
  POSITION_LIMIT: '최대 보유 종목 수 도달',
  NAV_UNAVAILABLE: '진입 기준 자산 평가 불가',
  CASH_INSUFFICIENT: '현금 부족',
  LOT_TOO_EXPENSIVE: '주당 가격이 진입 한도 초과',
}

function Observations({ state, detailLink }: { state: Experiment['state']; detailLink: DetailLink }) {
  const [filter, setFilter] = useState('')
  const observations = state.observations ?? []
  const reasons = [...new Set(observations.flatMap(item => item.reason ? [item.reason] : []))]
  const observationLabel = (value: string) => observationLabels[value] ?? value
  const completedReentries = observedReentrySignals(state.outbox)

  function isReentry(item: Observation) {
    const pendingOrder = state.pending[item.symbol]
    return item.order?.reentry || (pendingOrder?.reentry && pendingOrder.report_id === item.report_id &&
      pendingOrder.created_at === item.at) || (item.state === 'NEW' &&
      completedReentries.has(JSON.stringify([item.symbol, item.report_id, item.at])))
  }

  const filtered = observations.filter(item => {
    if (filter === 'deferred') return item.state.startsWith('DEFER') || item.state.startsWith('PENDING')
    if (filter === 'cancelled') return item.state === 'CANCELLED'
    if (filter === 'reentry') return item.state === 'DEFER_UNRESOLVED_INVALIDATION' || isReentry(item)
    return !filter || filter === item.reason
  }).reverse()

  return <Panel title="판단 로그" actions={<label className="cluster">
    판단 사유
    <select value={filter} onChange={event => setFilter(event.target.value)}>
      <option value="">전체</option>
      <option value="deferred">보류·대기</option>
      <option value="cancelled">취소</option>
      <option value="reentry">재진입 관련 기록</option>
      {reasons.map(reason => <option key={reason} value={reason}>{observationLabel(reason)}</option>)}
    </select>
  </label>}>
    {filtered.length ? <Table label="실험 판단 로그" columns={['시각 KST', '종목', '상태', '사유', '원본 판정', '보고서']}>
      {filtered.map((item, index) => {
        const report = item.report_id ?? item.order?.report_id
        return <tr key={item.at + item.symbol + item.state + index}>
          <td className="mono">{kst(item.at)}</td>
          <th scope="row" className="mono">{item.symbol}</th>
          <td>{observationLabel(item.state)}</td>
          <td>{item.reason ? observationLabel(item.reason) : isReentry(item) ? '재진입 주문' : '—'}</td>
          <td>{[item.original_decision, item.original_review_tier].filter(Boolean).join(' / ') || '—'}</td>
          <td>{report ? <a {...detailLink({ report })}>원본 보고서</a> : '—'}</td>
        </tr>
      })}
    </Table> : <Empty>{filter ? '선택한 조건의 판단 로그가 없습니다.' : '기록된 판단 로그가 없습니다.'}</Empty>}
  </Panel>
}

function DraftItem({ draft, detailLink }: { draft: AlertDraft; detailLink: DetailLink }) {
  const query = new URLSearchParams(draft.detail_path.split('?')[1] ?? '')
  const selection: DetailSelection = {}
  for (const key of ['event', 'trade', 'report'] as const) {
    const value = query.get(key)
    if (value !== null) selection[key] = value
  }
  const experiment = query.get('experiment')

  return <div className="stack">
    <div className="cluster">
      <Badge tone="warn">발송 대기 초안</Badge>
      <strong>{label(draft.kind)} {draft.symbol}</strong>
      <span className="muted">{kst(draft.at)}</span>
      {experiment && <a {...detailLink(selection, experiment)}>과거 상세</a>}
    </div>
    <details>
      <summary>초안 원문</summary>
      <pre className="report-text">{draft.text}</pre>
    </details>
  </div>
}

function EvidenceDrawer({ params, result, loading, error, onClose }: {
  params: URLSearchParams
  result?: Experiment
  loading: boolean
  error: string | null
  onClose: () => void
}) {
  const evidence = result ? resolveExperimentEvidence(result.state, params) : null
  const trade = evidence?.trade
  const event = evidence?.event
  const report = evidence?.report
  return <Drawer title="선택한 상세 근거" onClose={onClose}>
    {error && <ResourceState loading={false} error={error} />}
    {evidence ? <div className="stack">
      {trade && <section className="stack">
        <h3>가상 체결</h3>
        <div className="cluster"><Badge>{label(trade.side)}</Badge><strong className="mono">{trade.symbol} · {trade.quantity}주</strong></div>
        <p>{money(decimal(trade.price))} · 체결비용 {money(decimal(trade.fee))}</p>
        <p className="muted">신호 {kst(trade.signal_at)} → 체결 {kst(trade.filled_at)}</p>
      </section>}
      {event && <section className="stack">
        <h3>기준일 이벤트</h3>
        <p>{label(event.kind)} · {kst(event.at)}{event.symbol ? ' · ' + event.symbol : ''}</p>
      </section>}
      {report && <section className="stack">
        <h3>원본 보고서</h3>
        <p className="mono">{report.report_id}</p>
        <p className="muted">보고서 생성 {kst(report.at)} · 원본 판정 보존</p>
        <pre className="report-text">{JSON.stringify(report, null, 2)}</pre>
      </section>}
      {(trade || event) && <details>
        <summary>전체 근거와 시점 기록</summary>
        <pre className="report-text">{JSON.stringify({ experiment: result?.experiment_id, trade, event, report }, null, 2)}</pre>
      </details>}
    </div> : loading ? <ResourceState loading={true} error={null} /> : !error && <Empty>{missingDetail}</Empty>}
  </Drawer>
}

function OracleVerification() {
  const resource = useResource<VerificationStatus>('/api/status')
  const data = resource.data
  const inventory = data?.oracle_inventory
  const db = inventory?.tables
  const price = data?.actual_price_validation
  const financial = data?.actual_financial_validation
  const period = db?.ohlcv?.trade_date_range
  const pricePeriod = period?.length === 2 && period.every(Boolean) ? period.join(' ~ ') : '확인 불가'

  return <Panel title="Oracle 데이터 확보와 계산 검산">
    <details>
      <summary>자료 범위와 검산 결과</summary>
      <div className="stack">
        <ResourceState loading={resource.loading} error={resource.error} />
        {data && <>
          {db ? <section className="stack">
            <h3>Oracle 자료 범위</h3>
            <p className="muted">
              {inventory?.read_only === true ? '읽기 전용 조회' : '조회 방식 확인 불가'} {kst(data.checked_at_kst)}
              {data.production_db_changed === false && ' · 운영 DB 변경 없음'}
            </p>
            <p>가격 {count(db.ohlcv?.distinct_dates)}개 거래 날짜 · {count(db.ohlcv?.symbols)}종목 · {pricePeriod}</p>
            <p>재무 {count(db.financial_snapshots?.symbols)}종목 · 당시 관측·정정 이력 미인증</p>
          </section> : <Empty>Oracle 자료 위치 확인 대기</Empty>}
          {price?.results?.length ? <section className="stack">
            <h3>관측 가격 검산</h3>
            <p className="muted">기준 {kst(price.as_of)}</p>
            <Table label="관측 가격 검산" columns={['종목', '자료 기간', '거래 날짜', '독립 산식']}>
              {price.results.map(row => <tr key={row.symbol}>
                <th scope="row" className="mono">{row.symbol}</th>
                <td>{row.start} ~ {row.end}</td>
                <td className="mono">{row.distinct_trading_dates}일</td>
                <td>{row.source_features_equal_independent_formulas
                  ? row.calculation_dates_checked + '개 계산 날짜 · ' + row.fields_checked.length + '개 지표 일치'
                  : '확인 불가'}</td>
              </tr>)}
            </Table>
          </section> : null}
          {financial && <section className="stack">
            <h3>관측 재무 검산</h3>
            <p>{financial.symbol} · {financial.division} · 연간/두 반기 3기간 · {Object.keys(financial.checks).length}개 대조 {financial.all_equal ? '일치' : '불일치'}</p>
            <p>TTM 영업이익 <strong className="mono">{money(decimal(financial.values.ttm_operating_income))}</strong> · OCF는 원본 연간 기준</p>
            <p className="muted">관측 {kst(financial.observed_at)} · 과거 시점 인증 전</p>
          </section>}
          <p className="muted">실제 3년 시점별 자료는 미확보 상태입니다.</p>
        </>}
      </div>
    </details>
  </Panel>
}
