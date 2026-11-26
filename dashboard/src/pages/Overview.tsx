import { ArrowUpRight } from 'lucide-react'
import { evidenceMode, useCapabilities, useResource } from '../api/client'
import type { ReportList } from '../api/phaseB'
import type { OpsData } from './opsData'
import { AppLink } from '../components/AppLink'
import { Badge, Pnl, ResourceState } from '../components/ui'
import { finite, koreanUnit, kst, percent } from '../utils/formatters'
import type { DaytradeData } from './daytradeData'

type AccountList = { accounts: string[]; source: { state?: string; mode?: string } }
type HermesAccount = {
  current_assets: string | null
  current_return: string | null
  coverage: { mode?: string }
  metrics: { pending_trades: number }
  reports: { selected: { name: string; symbol: string } | null }[]
  position_quotes: Record<string, { freshness?: string }>
  watch_quotes: Record<string, { freshness?: string }>
}
type Experiment = { experiment_id: string; cost_bps: string | number; mode: string }
type ExperimentDetail = {
  experiment_id: string; cursor: number; total_events: number
  metrics: { max_drawdown?: string | null }
  state: { status?: string; equity: { return: string | null }[] }
}
type SourceStatus = { data_status: string; checked_at_kst?: string; oracle_inventory?: unknown; runner_errors?: string[] }

function ratio(value: unknown) {
  const number = finite(value)
  return number === null ? null : number * 100
}

export default function Overview() {
  const capability = useCapabilities()
  const phaseB = capability?.phase_b_available !== false
  const research = useResource<ReportList>(phaseB ? '/api/research/reports' : null)
  const swing = useResource<ReportList>(phaseB ? '/api/swing/reports' : null)
  const ops = useResource<OpsData>(phaseB ? '/api/ops/overview' : null)
  const researchReport = research.data?.latest
  const swingReport = swing.data?.latest
  const verification = ops.data?.verification
  const accounts = useResource<AccountList>('/api/paper-accounts')
  const account = useResource<HermesAccount>(accounts.data?.accounts[0]
    ? '/api/paper-accounts/' + encodeURIComponent(accounts.data.accounts[0]) : null)
  const daytrade = useResource<DaytradeData>('/api/daytrade/report')
  const experiments = useResource<Experiment[]>('/api/experiments')
  const latest = experiments.data?.find(item => Number(item.cost_bps) === 30)
  const experiment = useResource<ExperimentDetail>(latest ? '/api/experiments/' + encodeURIComponent(latest.experiment_id) : null)
  const status = useResource<SourceStatus>('/api/status')
  const day = daytrade.data?.report
  const hermes = account.data
  const run = experiment.data
  const recommendation = hermes?.reports[0]?.selected
  const states = Object.values(day?.sessions ?? {})
  const locks = day?.current_risk?.locks ?? []
  const positions = Object.keys(day?.positions ?? {}).length
  const stale = new Set([
    ...Object.entries(hermes?.watch_quotes ?? {}),
    ...Object.entries(hermes?.position_quotes ?? {}),
  ].filter(([, quote]) => quote.freshness === 'STALE').map(([symbol]) => symbol)).size
  const warningInputs = [status, daytrade, accounts, account]
  const warningsPending = warningInputs.some(resource => resource.loading)
  const warningsUnverified = warningInputs.some(resource => resource.error !== null)
  const dataStatus = verification?.data_status ?? status.data?.data_status
  const warnings = [
    stale ? 'Hermes 지연 시세 ' + stale + '종목' : null,
    locks.length ? '단타 활성 잠금 ' + locks.join(' · ') : null,
    positions ? '미청산 보유 ' + positions + '종목' : null,
    dataStatus && /INCOMPLETE|BLOCKED/.test(dataStatus) ? 'Oracle 3년 검산 자료 미확보' : null,
    status.data?.runner_errors?.length ? '실험 실행 오류 ' + status.data.runner_errors.length + '건' : null,
  ].filter(Boolean)

  return <div className="stack page-content">
    <header className="page-heading"><h1>통합 홈</h1><p>모의계좌, 당일 단타, 계산 검증과 리서치의 최근 기록을 확인합니다.</p></header>
    <section className="status-band cluster" aria-label="활성 경고 및 데이터 신선도">
      <strong>활성 경고</strong>
      {warnings.map(warning => <Badge key={warning} tone="warn">{warning}</Badge>)}
      {warningsUnverified && <span className="muted" role="status">일부 출처를 확인할 수 없습니다. 저장된 마지막 응답을 표시합니다.</span>}
      {warningsPending && !warningsUnverified && <span className="muted">기록 확인 중</span>}
      {!warnings.length && !warningsPending && !warningsUnverified && <span className="muted">확인된 활성 경고 없음</span>}
    </section>
    <div className="workspace-grid">
      <article className="overview-card">
        <header className="section-header"><h2>Hermes 모의계좌</h2><AppLink className="action-link" href="/hermes">계좌 보기 <ArrowUpRight size={15} /></AppLink></header>
        <ResourceState loading={accounts.loading || account.loading} error={accounts.error ?? account.error} />
        <div className="overview-card-content stack">
          <div className="cluster"><Badge tone={accounts.data?.source.state === 'CONNECTED' ? 'ok' : 'unknown'}>{accounts.data?.source.state ?? 'UNKNOWN'}</Badge>
            <Badge>{evidenceMode(hermes?.coverage.mode)}</Badge></div>
          <dl className="data-pairs"><dt>최근 1순위</dt><dd>{recommendation ? recommendation.name + ' (' + recommendation.symbol + ')' : '확인 불가'}</dd>
            <dt>총자산</dt><dd className="mono">{koreanUnit(hermes?.current_assets)}</dd><dt>수익률</dt>
            <dd><Pnl value={ratio(hermes?.current_return)} format={percent} /></dd>
            <dt>체결 대기</dt><dd>{hermes?.metrics.pending_trades == null ? '확인 불가' : hermes.metrics.pending_trades + '건'}</dd>
          </dl>
        </div>
      </article>
      <article className="overview-card">
        <header className="section-header"><h2>당일 단타</h2><AppLink className="action-link" href="/daytrade">세션 보기 <ArrowUpRight size={15} /></AppLink></header>
        <ResourceState loading={daytrade.loading} error={daytrade.error} />
        <div className="overview-card-content stack">
          <div className="cluster"><Badge tone={states.includes('UNFLATTENED') ? 'warn' : 'unknown'}>{states.length ? states.join(' · ') : 'UNKNOWN'}</Badge>
            <Badge>{evidenceMode(day?.data_source ?? day?.dataset_kind)}</Badge></div>
          <dl className="data-pairs"><dt>순손익</dt><dd><Pnl value={finite(day?.account.net_pnl)} format={koreanUnit} /></dd>
            <dt>수익률</dt><dd><Pnl value={finite(day?.account.return_pct)} format={percent} /></dd>
            <dt>완결 거래</dt><dd>{day ? day.account.completed_trades + '회' : '확인 불가'}</dd>
            <dt>위험 잠금</dt><dd>{day ? locks.join(' · ') || '없음' : '확인 불가'}</dd>
          </dl>
        </div>
      </article>
      <article className="overview-card">
        <header className="section-header"><h2>계산 검증 실험</h2><AppLink className="action-link" href={latest ? '/experiments?experiment=' + encodeURIComponent(latest.experiment_id) : '/experiments'}>실험 보기 <ArrowUpRight size={15} /></AppLink></header>
        <ResourceState loading={experiments.loading || experiment.loading} error={experiments.error ?? experiment.error} />
        <div className="overview-card-content stack">
          <p><Badge>{latest?.mode ?? 'UNKNOWN'}</Badge></p>
          <dl className="data-pairs"><dt>최근 30bps</dt><dd>{run?.state.status ?? '실험 확인 불가'}</dd>
            <dt>수익률</dt><dd><Pnl value={ratio(run?.state.equity.at(-1)?.return)} format={percent} /></dd>
            <dt>MDD</dt><dd><Pnl value={ratio(run?.metrics.max_drawdown)} format={percent} /></dd>
            <dt>처리 진행</dt><dd className="mono">{run ? run.cursor + ' / ' + run.total_events : '확인 불가'}</dd></dl>
        </div>
      </article>
      <article className="overview-card">
        <header className="section-header"><h2>통합 리서치·스윙</h2><AppLink className="action-link" href="/ops">운영 상태</AppLink></header>
        <div className="overview-card-content stack">
          <ResourceState loading={research.loading || swing.loading || ops.loading} error={research.error ?? swing.error ?? ops.error} />
          <div className="cluster">
            <AppLink className="action-link" href={researchReport ? '/research?report=' + encodeURIComponent(researchReport.report_id) : '/research'}>리서치 보고서</AppLink>
            <AppLink className="action-link" href={swingReport ? '/swing?report=' + encodeURIComponent(swingReport.report_id) : '/swing'}>스윙 보고서</AppLink>
          </div>
          <dl className="data-pairs">
            <dt>최근 리서치</dt><dd className="cluster"><span className="mono">{researchReport?.report_id ?? '저장 자료 확인 불가'}</span>
              {researchReport?.mode && <Badge>{evidenceMode(researchReport.mode)}</Badge>}</dd>
            <dt>매수 추천</dt><dd>{researchReport?.decision_counts?.BUY_RECOMMENDATION == null ? '확인 불가'
              : researchReport.decision_counts.BUY_RECOMMENDATION + '건'}</dd>
            <dt>최근 스윙</dt><dd className="cluster"><span className="mono">{swingReport?.report_id ?? '저장 자료 확인 불가'}</span>
              {swingReport?.mode && <Badge>{evidenceMode(swingReport.mode)}</Badge>}</dd>
            <dt>스윙 후보</dt><dd>{swingReport?.candidate_count == null ? '확인 불가' : swingReport.candidate_count + '건'}</dd>
            <dt>Oracle 커버리지</dt><dd>{dataStatus ?? '확인 불가'}</dd>
            <dt>자료 기준</dt><dd className="mono">{kst(verification?.checked_at_kst ?? status.data?.checked_at_kst)}</dd>
          </dl>
        </div>
      </article>
    </div>
  </div>
}
