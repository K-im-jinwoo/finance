export const comparisonCosts = [10, 30, 60] as const
export type ExperimentCost = typeof comparisonCosts[number]
export type DecimalValue = string | number | null
export type DetailQuery = 'trade' | 'event' | 'report'
const detailKeys = ['trade', 'event', 'report'] as const

export type ExperimentSummary = {
  experiment_id: string
  state: string
  cursor: number
  dataset_id: string
  cost_bps: string
  mode: string
}

type Trade = {
  trade_id: string
  report_id: string
  side: string
  symbol: string
  quantity: number
  price: DecimalValue
  fee: DecimalValue
  filled_at: string
  signal_at: string
}

export type ExperimentEvent = {
  event_id: string
  kind: string
  symbol?: string | null
  at: string
  detail?: unknown
}

type Report = {
  report_id: string
  at: string
  candidates: { symbol: string; decision: string; review_tier: string }[]
}

export type AlertDraft = ExperimentEvent & { detail_path: string; text: string }
type EquityRow = { at: string; total_assets: DecimalValue; return: DecimalValue }
type Position = { quantity: number; entry_price: DecimalValue; original_decision: string }
type Check = { code: string; status: string; reason?: string; evidence_id?: string; available_at?: string }
type PendingOrder = { side: string; report_id: string; created_at: string; reentry?: boolean }
export type Observation = {
  at: string
  report_id?: string
  symbol: string
  state: string
  reason?: string
  original_decision?: string
  original_review_tier?: string
  order?: PendingOrder
}

export type Experiment = {
  experiment_id: string
  cursor: number
  total_events: number
  coverage: { dataset_id: string; mode: string; supported_start: string | null; supported_end: string | null }
  policy: { initial_cash: DecimalValue; round_trip_bps: string }
  state: {
    status: string
    clock: string | null
    cash: DecimalValue
    equity: EquityRow[]
    positions: Record<string, Position>
    checks: Record<string, Check[]>
    invalidated: Record<string, { at: string; causes: Check[] }>
    trades: Trade[]
    reports: Report[]
    outbox: ExperimentEvent[]
    observations?: Observation[]
    pending: Record<string, PendingOrder>
  }
  metrics: {
    max_drawdown: DecimalValue
    total_cost: DecimalValue
    realized_pnl: DecimalValue
    cash_ratio: DecimalValue
    completed_round_trips: number
    open_positions: number
    win_rate: DecimalValue
    valuation_missing_count: number
  }
}

export type VerificationStatus = {
  checked_at_kst?: string
  oracle_inventory?: {
    read_only?: boolean
    tables?: {
      ohlcv?: { distinct_dates?: number; symbols?: number; trade_date_range?: (string | null)[] }
      financial_snapshots?: { symbols?: number }
    }
  } | null
  production_db_changed?: boolean
  actual_price_validation?: {
    as_of?: string
    results?: {
      symbol: string
      start?: string | null
      end?: string | null
      distinct_trading_dates?: number | null
      source_features_equal_independent_formulas?: boolean | null
      calculation_dates_checked?: number | null
      fields_checked?: string[] | null
    }[] | null
  } | null
  actual_financial_validation?: {
    symbol?: string | null
    division?: string | null
    observed_at?: string | null
    checks?: Record<string, boolean> | null
    all_equal?: boolean | null
    values?: { ttm_operating_income?: DecimalValue } | null
  } | null
}

// Invalidation drafts and BUY drafts are emitted in ledger order. A BUY clears
// the symbol's active invalidation, so only the first linked fill is a re-entry.
export function observedReentrySignals(events: ExperimentEvent[]) {
  const invalidated = new Set<string>()
  const signals = new Set<string>()
  for (const event of events) {
    if (!event.symbol) continue
    if (event.kind === 'INVALIDATION') invalidated.add(event.symbol)
    if (event.kind !== 'BUY' || !invalidated.delete(event.symbol)) continue
    const trade = event.detail as Pick<Trade, 'signal_at' | 'report_id'> | null | undefined
    if (typeof trade?.signal_at === 'string' && typeof trade.report_id === 'string') {
      signals.add(JSON.stringify([event.symbol, trade.report_id, trade.signal_at]))
    }
  }
  return signals
}

export function decimal(value: DecimalValue | undefined): number | null {
  if (value == null || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function percentagePoints(value: DecimalValue | undefined): number | null {
  const ratio = decimal(value)
  return ratio === null ? null : ratio * 100
}

export function equityPoints(rows: Pick<EquityRow, 'at' | 'total_assets'>[], initialCash: DecimalValue) {
  let peak = decimal(initialCash) ?? 0
  return rows.map(row => {
    const equity = decimal(row.total_assets)
    if (equity === null) return { time: row.at, equity: null, drawdown: null }
    peak = Math.max(peak, equity)
    return { time: row.at, equity, drawdown: peak > 0 ? (equity / peak - 1) * 100 : null }
  })
}

export function experimentQuery(current: URLSearchParams, changes: Record<string, string | null>, clearDetails = false) {
  const next = new URLSearchParams(current)
  if (clearDetails) for (const key of detailKeys) next.delete(key)
  for (const [key, value] of Object.entries(changes)) {
    if (value === null) next.delete(key)
    else next.set(key, value)
  }
  return next
}

export function hasExperimentEvidence(params: URLSearchParams) {
  return detailKeys.some(key => params.has(key))
}

export function resolveExperimentEvidence<
  T extends { trade_id: string; report_id: string },
  E extends { event_id: string; detail?: unknown },
  R extends { report_id: string },
>(records: { trades: T[]; outbox: E[]; reports: R[] }, params: URLSearchParams) {
  const trade = records.trades.find(item => item.trade_id === params.get('trade'))
  const event = records.outbox.find(item => item.event_id === params.get('event'))
  const report = records.reports.find(item => item.report_id === params.get('report'))
  const eventDetail = event?.detail as { trade_id?: string; report_id?: string } | null | undefined
  if (
    (params.has('trade') && !trade) ||
    (params.has('event') && !event) ||
    (params.has('report') && !report) ||
    (trade && report && trade.report_id !== report.report_id) ||
    (trade && event && eventDetail?.trade_id !== trade.trade_id) ||
    (event && report && eventDetail?.report_id !== report.report_id)
  ) return null
  return { trade, event, report }
}
