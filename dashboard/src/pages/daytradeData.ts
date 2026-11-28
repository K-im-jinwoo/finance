import type { PublicSettings } from './opsData'
import type { ChartPoint } from '../components/Charts'
import { finite } from '../utils/formatters'
import { labelForCode, readableLog } from '../utils/labels'

export type Amount = string | number | null
type Dated = { at: string }
type ListedSymbol = { symbol?: string | null; name?: string }
type Reasoned = { reasons?: string[]; exit_reason?: string | null }
type Candidate = ListedSymbol & { rank_turnover?: number; rank_volume?: number; turnover?: Amount; spread_bps?: Amount;
  entry_blocked?: boolean; selection_reasons?: string[]; reasons?: string[]; reason?: string }
export type Scan = Dated & { selected: Candidate[]; rejected: Candidate[]; rejected_count?: number; rejected_counts?: Record<string, number> }
export type Signal = Dated & ListedSymbol & Reasoned & { action: string; score: Amount }
type Mark = Dated & { equity: Amount; phase: string; locks?: string[]; diagnostic_truncation?: boolean }
type Fill = Dated & ListedSymbol & { event_id?: string; side: string; quantity: number;
  price: Amount; fee: Amount; tax: Amount; realized_pnl: Amount; signal?: Reasoned | null }
type CompletedTrade = ListedSymbol & {
  entry_at: string; exit_at: string; buy_quantity: number; average_buy_price: Amount; average_sell_price: Amount
  fees: Amount; tax: Amount; realized_pnl: Amount
}
export type DaytradeReport = {
  profile: string
  dataset_kind: string
  data_source?: string
  mode: string
  paper_ready: boolean
  cost_policy_status?: string
  cost_policy_notice?: string
  account: {
    initial_capital: Amount; cash: Amount; final_equity: Amount; valuation_status: string
    net_pnl: Amount; realized_pnl_net: Amount; return_pct: Amount; completed_trades: number
    win_rate_pct: Amount; mdd_pct: Amount; mdd_observed_pct?: Amount; mdd_uncertain: boolean
    missing_valuation_points?: number
  }
  sessions: Record<string, string>
  positions: Record<string, { quantity: number; cost: Amount }>
  completed_trades: CompletedTrade[]
  equity_curve: Mark[]
  fills: Fill[]
  scan_history: Scan[]
  signals: Signal[]
  risk_events: (Dated & { phase: string; locks: string[] })[]
  quality_events: (Dated & ListedSymbol & { issues: string[] })[]
  decisions: (Dated & ListedSymbol & { status: string; reason: string })[]
  current_risk?: { phase: string; locks: string[]; entry_allowed: boolean }
  symbol_names?: Record<string, string>
}
export type DaytradeData = {
  present: boolean; sample_available: boolean; report: DaytradeReport | null; markdown?: string | null
  reason?: string; live_settings?: PublicSettings
}

export const SIGNAL_LIMIT = 30
export const RISK_LIMIT = 15

const labels: Record<string, string> = {
  BUY: '매수', SELL: '매도', HOLD: '대기', CLOSED: '마감 · 보유 없음',
  UNFLATTENED: '미청산 보유', OBSERVING: '장중 모니터링', OPEN: '신규 진입 가능 시간',
  ENTRY_CLOSED: '신규 진입 마감', TIME_EXITING: '시간 청산 중',
  LOSS_LOCKED: '일손실 한도 잠금', DATA_DEGRADED: '데이터 품질 잠금',
  RATE_LIMIT_DEGRADED: '호출 제한 잠금', PRIOR_DAY_UNFLATTENED: '전일 미청산 잠금',
  TIME_EXIT: '시간 청산', LOSS_EXIT: '일손실 한도 청산', TAKE_PROFIT: '익절', TRAILING_STOP: '트레일링 익절',
  PARTIAL_TAKE_PROFIT: '1차 분할 익절', BREAKEVEN_STOP: '본절 보호 청산',
  PARTIAL_TAKE_PROFIT_CANCELLED_FOR_EXIT: '전량 청산으로 분할 익절 취소',
  STOP_LOSS: '손절', MOMENTUM_WEAK: '모멘텀 약화', VWAP_BREAKOUT: '분봉 근사 VWAP 돌파',
  RISING_CLOSE: '완료 분봉 상승', VOLUME_SPIKE: '완료 분봉 거래량 급증', BID_DEPTH: '매수호가 잔량 충족',
  HALTED: '거래정지', DELISTING: '상장폐지 위험', WARNING_OR_CAUTION: '경고·주의',
  MAJOR_NEGATIVE_DISCLOSURE: '중대 악재 공시', LOW_TURNOVER: '거래대금 부족', LOW_PRICE: '최소 가격 미달',
  WIDE_SPREAD: '매수·매도 호가차 초과', WATCH_LIMIT: '순위 외 제외 (상위 감시 정원 초과)',
  TURNOVER_ELIGIBLE: '거래대금 조건 충족', TURNOVER_RANKED: '거래대금 순위 편입',
  VOLUME_RANKED: '거래량 순위 편입', QUOTE_ELIGIBLE: '호가 조건 충족',
  QUOTE_UNAVAILABLE_WATCH_ONLY: '호가 미확보 · 감시만 가능', RISK_METADATA_UNAVAILABLE: '위험 메타 미확보',
  RISK_METADATA_PROVIDED: '위험 메타 제공', ENTRY_NOT_ALLOWED: '시간 또는 위험 상태로 신규 매수 차단',
  STALE_RANKINGS: '랭킹 지연', STALE_ORDERBOOK: '호가 지연', STALE_CANDLES: '분봉 지연',
  STALE_TRADE: '체결 시세 지연', SOURCE_TIMESTAMP_UNAVAILABLE: '원천 시각 미제공',
  EMPTY_ORDERBOOK: '호가 없음', CROSSED_ORDERBOOK: '호가 역전', ORDERBOOK_UNAVAILABLE: '호가 미확보',
  POSITION_HELD: '기존 보유 유지', COST_BASIS_UNAVAILABLE: '매입 원가 미확보',
  CANDLES_UNAVAILABLE: '분봉 미확보', INSUFFICIENT_CANDLES: '완료 분봉 부족',
  VOLUME_BASELINE_UNAVAILABLE: '비교 거래량 미확보', FRESHNESS_POLICY_UNSET: '시세 신선도 기준 미설정',
}

export function labelFor(value?: string | null) {
  return value ? labels[value] ?? labelForCode(value) : '확인 불가'
}

export function reasonsFor(values?: string[]) {
  return values?.length ? values.map(labelFor).join(' · ') : '없음'
}

export function symbolLabel(names: Record<string, string>, code?: string | null, provided?: string) {
  if (!code || code === 'ALL') return '시장 전체'
  const name = names[code] || (provided && provided !== code ? provided : null)
  return (name ? readableLog(name) : '종목명 확인 중') + ' (' + code + ')'
}

export function newest<T extends Dated>(rows: T[]) {
  return [...rows].sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
}

export function equityPoints(marks: Mark[], initialCapital: Amount): ChartPoint[] {
  let peak = finite(initialCapital)
  return marks.map(mark => {
    const equity = mark.diagnostic_truncation ? null : finite(mark.equity)
    if (equity !== null) peak = Math.max(peak ?? equity, equity)
    const risk = mark.locks?.length ? reasonsFor(mark.locks)
      : ['LOSS_LOCKED', 'DATA_DEGRADED', 'RATE_LIMIT_DEGRADED', 'PRIOR_DAY_UNFLATTENED', 'TIME_EXITING'].includes(mark.phase)
        ? labelFor(mark.phase) : null
    return {
      time: mark.at,
      equity,
      omitted: mark.diagnostic_truncation,
      drawdown: equity === null || !peak || peak <= 0 ? null : (equity - peak) * 100 / peak,
      risk,
    }
  })
}
