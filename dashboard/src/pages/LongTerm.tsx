import { useState } from 'react'
import { postJson, useCapabilities, useRefresh, useResource } from '../api/client'
import { AppLink } from '../components/AppLink'
import { EquityDrawdownChart } from '../components/Charts'
import { Badge, Empty, MetricGrid, Panel, Pnl, ResourceState, Skeleton, SummaryMoney, Table } from '../components/ui'
import { finite, kst, money, numeric, percent, summaryMoney } from '../utils/formatters'
import { evidenceText, labelForCode, reportName } from '../utils/labels'
import '../styles/longterm.css'

type Scalar = string | null
type Report = {
  report_id: string
  as_of: string
  signal_date: string
  mode?: string
  recommendation?: { symbol: string; name: string; rank: number } | null
  actions: { symbol: string; action: string; reason?: string }[]
}
type Mark = {
  date: Scalar; closed_at?: string; total_assets: Scalar; return: Scalar; cash: Scalar
  drawdown: Scalar; max_drawdown: Scalar; unrealized_pnl: Scalar; realized_pnl: Scalar
}
export type LongTermOverview = {
  status: 'NO_ACCOUNT' | 'RUNNING' | 'PAUSED'
  policy: { initial_cash: string; max_positions: number; round_trip_bps: number }
  updated_at: Scalar
  latest_report: Report | null
  position_count: number
  pending_count: number
  risk_exit_count: number
  missing_close_count: number
  metrics: (Mark & { cash_weight: Scalar }) | null
}
type Position = {
  symbol: string; name: string; quantity: number; entry_date: string; entry_price: string
  entry_report_id: string; latest_report_id: Scalar; last_close?: string; last_close_date?: string
  market_value: Scalar; weight: Scalar; return: Scalar; unrealized_pnl: Scalar
  review_status: string; decision: Scalar; close_missing?: boolean
}
type Order = {
  id: number; symbol: string; name: string; side: 'BUY' | 'SELL'; report_id: string
  signal_date: string; status: 'PENDING' | 'DEFER' | 'FILLED' | 'CANCELLED'
  reason: string; signal_reason: string; cancellation_report_id?: string
}
type Fill = {
  id: number; symbol: string; name: string; side: 'BUY' | 'SELL'; report_id: string
  trade_date: string; closed_at?: string; price: string; quantity: number; fee: string; realized_pnl: Scalar
}
type LongTermAccount = LongTermOverview & {
  positions: Position[]; pending: Order[]; orders: Order[]; fills: Fill[]; equity: Mark[]; reports: Report[]
}

const statusLabels = { RUNNING: '운영 중', PAUSED: '일시중지', NO_ACCOUNT: '계좌 미생성' }
const labels: Record<string, string> = {
  ACCOUNT_PAUSED: '계좌 일시중지', BUY_CANCELLED: '매수 취소', BUY_QUEUED: '매수 대기',
  SELL_QUEUED: '매도 대기', BUY_RECOMMENDATION: '매수 추천', WATCH: '관찰',
  VERIFY_PENDING: '검증 대기', EXCLUDED: '제외 판정', NOT_APPLICABLE: '평가 대상 아님',
  HOLD_NO_EXIT_TRIGGER: '보유 유지', REVIEW_REQUIRED: '검토 필요', MAINTAIN: '보유 유지',
  PENDING_MAINTAIN: '기존 매수 대기 유지', PAUSED: '일시중지 중 신규 매수 보류',
  RISK_BLOCKED: '리스크로 신규 매수 차단', RISK_PENALTY: '리스크 감점', RISK_SIGNAL: '리스크 감지',
  CANCELLED_BY_RISK_OR_EXCLUSION: '리스크 또는 제외 판정으로 취소', POSITION_LIMIT: '보유 한도 도달',
  INSUFFICIENT_CASH: '매수 가능 금액 부족', MISSING_POSITION_CLOSE: '보유 종목 종가 누락',
  PENDING_UNTRADABLE_CLOSE: '거래 가능한 공식 종가 대기',
}

export function LongTermStatus({ status }: { status: LongTermOverview['status'] | undefined }) {
  return <Badge tone={status === 'RUNNING' ? 'ok' : status === 'PAUSED' ? 'warn' : 'unknown'}>
    {status ? statusLabels[status] : '상태 확인 불가'}
  </Badge>
}

export function ratioPercent(value: unknown) {
  const number = finite(value)
  return number === null ? null : Number((number * 100).toFixed(2))
}

function reportLink(report: string, symbol?: string) {
  return '/research?' + new URLSearchParams(symbol ? { report, symbol } : { report })
}

function TradeSide({ side }: { side: Order['side'] }) {
  return <Badge tone={side === 'SELL' ? 'warn' : 'unknown'}>{side === 'BUY' ? '매수' : '매도'}</Badge>
}

function Positions({ positions }: { positions: Position[] }) {
  return <Panel title="보유 종목">
    {positions.length ? <Table label="보유 종목" columns={['종목', '편입일 · 편입가', '최근 종가', '수량', '평가금액 · 비중', '평가손익 · 수익률', '최근 리서치']}>
      {positions.map(position => <tr key={position.symbol}>
        <th scope="row"><AppLink className="action-link longterm-symbol" href={reportLink(position.entry_report_id, position.symbol)}>
          <strong>{position.name}</strong><span className="mono">{position.symbol}</span>
        </AppLink></th>
        <td><div className="mono">{position.entry_date}</div><div className="mono muted">{money(position.entry_price)}</div></td>
        <td><div className="mono">{money(position.last_close)}</div>
          {position.last_close_date && <div className="mono muted">{position.last_close_date}</div>}
          {position.close_missing && <Badge tone="warn">당일 종가 누락</Badge>}</td>
        <td className="mono">{numeric(position.quantity, '주')}</td>
        <td><div className="mono">{money(position.market_value)}</div><div className="mono muted">{percent(ratioPercent(position.weight))}</div></td>
        <td><Pnl value={finite(position.unrealized_pnl)} format={money} /><div><Pnl value={ratioPercent(position.return)} format={percent} /></div></td>
        <td><div className="cluster">
          {position.decision && <Badge tone={position.decision === 'EXCLUDED' ? 'warn' : 'unknown'}>{labels[position.decision] ?? labelForCode(position.decision)}</Badge>}
          {position.review_status !== 'MAINTAIN' && position.review_status !== position.decision &&
            <Badge tone={position.review_status === 'HOLD_NO_EXIT_TRIGGER' ? 'unknown' : 'warn'}>
              {labels[position.review_status] ?? labelForCode(position.review_status)}
            </Badge>}
        </div>
          {position.latest_report_id && position.latest_report_id !== position.entry_report_id &&
            <AppLink className="action-link" href={reportLink(position.latest_report_id)}>최근 보고서</AppLink>}</td>
      </tr>)}
    </Table> : <Empty>현재 보유한 종목이 없습니다.</Empty>}
  </Panel>
}

function PendingOrders({ orders }: { orders: Order[] }) {
  return <Panel title="체결 대기">
    {orders.length ? <Table label="체결 대기" columns={['구분', '종목', '신호 기준일', '상태 · 사유']}>
      {orders.map(order => <tr key={order.id}>
        <td><TradeSide side={order.side} /></td>
        <th scope="row">{order.name} <span className="mono muted">{order.symbol}</span></th>
        <td className="mono">{order.signal_date}</td>
        <td><div className="cluster"><Badge tone={order.status === 'DEFER' ? 'warn' : 'unknown'}>{order.status === 'DEFER' ? '보류' : '대기'}</Badge>
          {labels[order.signal_reason] ?? evidenceText(order.signal_reason)}</div>
          {order.reason !== order.signal_reason && <div className="muted">{labels[order.reason] ?? evidenceText(order.reason)}</div>}</td>
      </tr>)}
    </Table> : <Empty>체결을 기다리는 주문이 없습니다.</Empty>}
  </Panel>
}

function TradeHistory({ fills, orders }: { fills: Fill[]; orders: Order[] }) {
  const cancellations = orders.filter(order => order.status === 'CANCELLED').reverse()
  return <Panel title="체결·청산 이력">
    {fills.length ? <Table label="체결·청산 이력" columns={['체결일', '구분', '종목', '종가 · 수량', '거래비용', '실현손익']}>
      {[...fills].reverse().map(fill => <tr key={fill.id}>
        <td className="mono">{fill.trade_date}</td>
        <td><TradeSide side={fill.side} /></td>
        <th scope="row">{fill.name} <span className="mono muted">{fill.symbol}</span></th>
        <td className="mono">{money(fill.price)} · {numeric(fill.quantity, '주')}</td>
        <td className="mono">{money(fill.fee)}</td>
        <td>{fill.side === 'SELL' ? <Pnl value={finite(fill.realized_pnl)} format={money} /> : '—'}</td>
      </tr>)}
    </Table> : <Empty>아직 체결된 거래가 없습니다.</Empty>}
    {!!cancellations.length && <details>
      <summary>취소된 매수 {cancellations.length}건</summary>
      <Table label="취소된 매수" columns={['종목', '신호 기준일', '취소 사유']}>
        {cancellations.map(order => <tr key={order.id}>
          <th scope="row">{order.name} <span className="mono muted">{order.symbol}</span></th>
          <td className="mono">{order.signal_date}</td>
          <td>{labels[order.reason] ?? evidenceText(order.reason)}{order.cancellation_report_id &&
            <div><AppLink className="action-link" href={reportLink(order.cancellation_report_id)}>취소 근거 보고서</AppLink></div>}</td>
        </tr>)}
      </Table>
    </details>}
  </Panel>
}

function ReportAudit({ reports }: { reports: Report[] }) {
  return <Panel title="리서치 반영 이력">
    {reports.length ? <Table label="리서치 반영 이력" columns={['보고서', '판정 기준', '반영 결과']}>
      {reports.map(report => <tr key={report.report_id}>
        <th scope="row"><AppLink className="action-link mono" href={reportLink(report.report_id)} title={report.report_id}>{reportName(report, 'research', reports)}</AppLink></th>
        <td className="mono">{kst(report.as_of)}</td>
        <td>{report.actions.length ? <details>
          <summary>판정 {report.actions.length}건</summary>
          <ul className="reason-list">
            {report.actions.map(({ symbol, action, reason }) => <li key={symbol + action}>
              <span className="mono">{symbol}</span> {labels[action] ?? action}
              {reason && ' · ' + (labels[reason] ?? evidenceText(reason))}
            </li>)}
          </ul>
        </details> : '주문 또는 보유 변경 없음'}</td>
      </tr>)}
    </Table> : <Empty>원장에 반영된 통합 리서치 보고서가 없습니다.</Empty>}
  </Panel>
}

const accountPath = '/api/longterm/account'
type Action = 'init' | 'pause' | 'resume' | 'refresh'

export default function LongTerm() {
  const account = useResource<LongTermAccount>(accountPath)
  const capability = useCapabilities()
  const refresh = useRefresh()
  const [busy, setBusy] = useState<Action | null>(null)
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null)
  const data = account.data
  const metrics = data?.metrics
  const policy = data?.policy

  async function apply(action: Action) {
    setBusy(action)
    setFeedback(null)
    try {
      await postJson<LongTermAccount>(action === 'refresh' ? '/api/longterm/refresh' : accountPath + '/' + action, {})
      setFeedback({ error: false, text: {
        init: '장기 모의계좌를 생성했습니다.', refresh: '원장을 갱신했습니다.',
        pause: '신규 매수와 대기 매수의 체결을 중지했습니다.', resume: '매수를 재개했습니다.',
      }[action] })
      refresh()
    } catch (error) {
      setFeedback({ error: true, text: error instanceof Error ? error.message : '계좌 작업을 완료하지 못했습니다.' })
    } finally {
      setBusy(null)
    }
  }

  return <div className="stack page-content longterm-page">
    <header className="page-heading"><h1>장기 모의계좌</h1></header>
    <section className="status-band longterm-status" aria-label="계좌 상태 및 제어">
      {account.loading ? <Skeleton label="장기 계좌 상태 불러오는 중" /> : <div className="summary-line">
        {data && <span><LongTermStatus status={data.status} /></span>}
        {policy && <>
          <span>다음 거래일 종가</span><span>최대 {policy.max_positions}종목</span>
          <span className="amount">수수료 {(policy.round_trip_bps / 100).toFixed(2)}%</span>
        </>}
      </div>}
      {data?.updated_at && <span className="mono muted">최근 갱신 {kst(data.updated_at)}</span>}
      {capability?.read_only === false && data && <div className="cluster longterm-actions">
        {data.status === 'NO_ACCOUNT' ? <button disabled={busy !== null} onClick={() => void apply('init')}>계좌 시작</button> : <>
          <button disabled={busy !== null} onClick={() => void apply(data.status === 'RUNNING' ? 'pause' : 'resume')}>
            {data.status === 'RUNNING' ? '일시중지' : '재개'}
          </button>
          <button disabled={busy !== null} onClick={() => void apply('refresh')}>원장 갱신</button>
        </>}
      </div>}
    </section>
    <ResourceState loading={false} error={account.error} />
    {feedback && <p className={feedback.error ? 'notice notice-danger' : 'muted'} role="status">{feedback.text}</p>}
    {data?.status === 'NO_ACCOUNT' && <Empty>생성된 장기 모의계좌가 없습니다. 첫 계좌는 생성 시점의 최신 통합 리서치부터 반영합니다.</Empty>}
    {data && data.status !== 'NO_ACCOUNT' && <>
      {(data.risk_exit_count > 0 || data.missing_close_count > 0) && <div className="cluster" aria-label="장기 계좌 경고">
        {data.risk_exit_count > 0 && <Badge tone="warn">리스크 청산 대기 {data.risk_exit_count}건</Badge>}
        {data.missing_close_count > 0 && <Badge tone="warn">종가 누락 {data.missing_close_count}종목</Badge>}
      </div>}
      <MetricGrid metrics={[
        { label: '총자산', value: <SummaryMoney value={metrics?.total_assets} />, detail: metrics?.date },
        { label: '누적 수익률', value: <Pnl value={ratioPercent(metrics?.return)} format={percent} /> },
        { label: '평가손익', value: <Pnl value={finite(metrics?.unrealized_pnl)} format={summaryMoney} /> },
        { label: '실현손익', value: <Pnl value={finite(metrics?.realized_pnl)} format={summaryMoney} /> },
        { label: '현금', value: <SummaryMoney value={metrics?.cash} />, detail: '비중 ' + percent(ratioPercent(metrics?.cash_weight)) },
        { label: '최대 낙폭 (MDD)', value: <Pnl value={ratioPercent(metrics?.max_drawdown)} format={percent} /> },
      ]} />
      <Panel title="일별 자산 및 낙폭">
        <EquityDrawdownChart label="장기 모의계좌 일별 자산 및 낙폭"
          points={data.equity.map(mark => ({ time: mark.closed_at ?? mark.date ?? '', equity: finite(mark.total_assets), drawdown: ratioPercent(mark.drawdown) }))}
          fills={data.fills.map(fill => ({ time: fill.closed_at ?? fill.trade_date, label: fill.name, side: fill.side }))} />
      </Panel>
      <Positions positions={data.positions} />
      <PendingOrders orders={data.pending} />
      <TradeHistory fills={data.fills} orders={data.orders} />
      <ReportAudit reports={data.reports} />
    </>}
  </div>
}
