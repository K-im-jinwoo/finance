import { ArrowUpRight } from 'lucide-react'
import { useCapabilities, useResource } from '../api/client'
import type { ReportList } from '../api/phaseB'
import { AppLink } from '../components/AppLink'
import { Badge, Empty, Pnl, ResourceState, Skeleton, SummaryMoney } from '../components/ui'
import { finite, kst, numeric, percent, summaryMoney } from '../utils/formatters'
import { evidenceLabel, labelForCode, reportName } from '../utils/labels'
import { labelFor, reasonsFor } from './daytradeData'
import type { DaytradeData } from './daytradeData'
import { LongTermStatus, ratioPercent } from './LongTerm'
import type { LongTermOverview } from './LongTerm'

type Experiment = { experiment_id: string; cost_bps: string | number; mode: string }
type ExperimentDetail = {
  cursor: number; total_events: number
  metrics: { max_drawdown?: string | null }
  state: { status?: string; equity: { return: string | null }[] }
}

export default function Overview() {
  const capability = useCapabilities()
  const phaseB = capability?.phase_b_available !== false
  const research = useResource<ReportList>(phaseB ? '/api/research/reports' : null)
  const swing = useResource<ReportList>(phaseB ? '/api/swing/reports' : null)
  const account = useResource<LongTermOverview>('/api/longterm/overview')
  const daytrade = useResource<DaytradeData>('/api/daytrade/report')
  const experimentsEnabled = capability?.read_only === false && capability.routes.includes('/experiments')
  const experiments = useResource<Experiment[]>(experimentsEnabled ? '/api/experiments' : null)
  const latest = experiments.data?.find(item => Number(item.cost_bps) === 30)
  const experiment = useResource<ExperimentDetail>(latest ? '/api/experiments/' + encodeURIComponent(latest.experiment_id) : null)
  const status = useResource<{ runner_errors?: Record<string, string> }>(experimentsEnabled ? '/api/status' : null)
  const day = daytrade.data?.report
  const longterm = account.data
  const run = experiment.data
  const recommendation = longterm?.latest_report?.recommendation
  const states = [...new Set(Object.values(day?.sessions ?? {}))]
  const locks = day?.current_risk?.locks ?? []
  const positions = Object.keys(day?.positions ?? {}).length
  const runnerErrors = Object.keys(status.data?.runner_errors ?? {}).length
  const daySource = evidenceLabel(day?.data_source ?? day?.dataset_kind)
  const warnings = [
    longterm?.risk_exit_count ? '장기 계좌 리스크 청산 대기 ' + longterm.risk_exit_count + '건' : null,
    longterm?.missing_close_count ? '장기 계좌 종가 누락 ' + longterm.missing_close_count + '종목' : null,
    locks.length ? '단타 위험 잠금 · ' + reasonsFor(locks) : null,
    positions ? '미청산 보유 ' + positions + '종목' : null,
    runnerErrors ? '실험 실행 오류 ' + runnerErrors + '건' : null,
  ].filter(Boolean)
  const reports = [
    { kind: 'research', resource: research, path: '/research', fallback: '리서치 보고서', countLabel: '매수 추천',
      count: research.data?.latest?.decision_counts?.BUY_RECOMMENDATION },
    { kind: 'swing', resource: swing, path: '/swing', fallback: '스윙 보고서', countLabel: '스윙 후보',
      count: swing.data?.latest?.candidate_count },
  ] as const
  const latestReportAt = reports.map(({ resource }) => resource.data?.latest?.as_of)
    .filter((at): at is string => Boolean(at) && Number.isFinite(Date.parse(at!))).sort((a, b) => Date.parse(b) - Date.parse(a))[0]

  return <div className="stack page-content">
    <header className="page-heading"><h1>통합 홈</h1></header>
    {warnings.length > 0 && <section className="notice notice-warn stack" aria-label="확인이 필요한 사항">
      <strong>확인이 필요한 사항</strong>
      <ul>{warnings.map(warning => <li key={warning}>{warning}</li>)}</ul>
    </section>}
    <div className="workspace-grid overview-grid">
      <article className="overview-card">
        <header className="section-header"><h2>장기 모의계좌</h2><AppLink className="action-link" href="/longterm">계좌 보기 <ArrowUpRight size={15} /></AppLink></header>
        <div className="overview-card-content stack">
          <ResourceState loading={false} error={account.error} />
          {account.loading ? <Skeleton label="장기 계좌 불러오는 중" /> : <>
            {longterm && <div><LongTermStatus status={longterm.status} /></div>}
            <dl className="data-pairs">
              <dt>최근 1순위</dt><dd>{recommendation ? recommendation.name + ' (' + recommendation.symbol + ')'
                : longterm?.latest_report ? '매수 추천 없음' : '확인 불가'}</dd>
              <dt>총자산</dt><dd><SummaryMoney value={longterm?.metrics?.total_assets} /></dd>
              <dt>누적 수익률</dt><dd><Pnl value={ratioPercent(longterm?.metrics?.return)} format={percent} /></dd>
              <dt>보유 종목</dt><dd>{numeric(longterm?.position_count, '종목')}</dd>
              <dt>체결 대기</dt><dd>{numeric(longterm?.pending_count, '건')}</dd>
            </dl>
          </>}
        </div>
      </article>
      <article className="overview-card">
        <header className="section-header"><h2>당일 단타</h2><AppLink className="action-link" href="/daytrade">세션 보기 <ArrowUpRight size={15} /></AppLink></header>
        <div className="overview-card-content stack">
          <ResourceState loading={false} error={daytrade.error} />
          {daytrade.loading ? <Skeleton label="당일 단타 불러오는 중" />
            : !day ? <Empty>저장된 단타 원장이 없습니다.</Empty> : <>
              <div className="summary-line">
                {states.length > 0 && <span>{states.map(labelFor).join(' · ')}</span>}
                {daySource && <span className="muted">{daySource}</span>}
              </div>
              <dl className="data-pairs">
                <dt>순손익</dt><dd><Pnl value={finite(day.account.net_pnl)} format={summaryMoney} /></dd>
                <dt>수익률</dt><dd><Pnl value={finite(day.account.return_pct)} format={percent} /></dd>
                <dt>완결 거래</dt><dd>{numeric(day.account.completed_trades, '회')}</dd>
                <dt>위험 잠금</dt><dd>{locks.length ? reasonsFor(locks) : day.current_risk ? '없음' : '확인 불가'}</dd>
              </dl>
            </>}
        </div>
      </article>
      {phaseB && <article className="overview-card">
        <header className="section-header"><h2>통합 리서치·스윙</h2><AppLink className="action-link" href="/ops">운영 상태</AppLink></header>
        <div className="overview-card-content stack">
          <ResourceState loading={false} error={research.error ?? swing.error} />
          {research.loading || swing.loading ? <Skeleton label="최근 보고서 불러오는 중" /> : <div className="overview-reports">
            {reports.map(({ kind, resource, path, fallback, countLabel, count }) => {
              const report = resource.data?.latest
              return <div className="overview-report" key={kind}>
                <AppLink className="action-link" href={report ? path + '?report=' + encodeURIComponent(report.report_id) : path}
                  title={report?.report_id}>{report ? reportName(report, kind, resource.data?.reports) : fallback}</AppLink>
                <p>{countLabel} {numeric(count, '건')}</p>
              </div>
            })}
          </div>}
          {latestReportAt && <p className="muted">최근 보고서 업데이트 <time dateTime={latestReportAt}>{kst(latestReportAt)}</time></p>}
        </div>
      </article>}
      {experimentsEnabled && <article className="overview-card">
        <header className="section-header"><h2>계산 검증 실험</h2><AppLink className="action-link" href={latest ? '/experiments?experiment=' + encodeURIComponent(latest.experiment_id) : '/experiments'}>실험 보기 <ArrowUpRight size={15} /></AppLink></header>
        <div className="overview-card-content stack">
          <ResourceState loading={false} error={experiments.error ?? experiment.error} />
          {experiments.loading || experiment.loading ? <Skeleton label="검증 실험 불러오는 중" /> : <>
            {evidenceLabel(latest?.mode) && <div><Badge>{evidenceLabel(latest?.mode)}</Badge></div>}
            <dl className="data-pairs"><dt>최근 30bp 실험</dt><dd>{labelForCode(run?.state.status, '실험 확인 불가')}</dd>
              <dt>수익률</dt><dd><Pnl value={ratioPercent(run?.state.equity.at(-1)?.return)} format={percent} /></dd>
              <dt>최대 낙폭</dt><dd><Pnl value={ratioPercent(run?.metrics.max_drawdown)} format={percent} /></dd>
              <dt>처리 진행</dt><dd className="mono">{run ? run.cursor + ' / ' + run.total_events : '확인 불가'}</dd></dl>
          </>}
        </div>
      </article>}
    </div>
  </div>
}
