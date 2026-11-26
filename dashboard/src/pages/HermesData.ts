import { finite } from '../utils/formatters'

type DecimalValue = string | number | null | undefined

export interface Candidate {
  rank: number
  name: string
  symbol: string
  original_review_tier: string | null
  original_decision: string | null
  reason: string | null
  conditions: string | null
  invalidation: string | null
}

export interface HermesReport {
  key: string
  report_id: string | null
  author: string
  source: string
  published_at: string
  retrieved_at: string
  content_sha256: string
  content: string
  candidates: Candidate[]
  selection_status: string
  selected: Candidate | null
}

export interface HermesSource {
  state?: string
  mode?: string
  last_success_at?: string
  last_attempt_at?: string
  error?: string | null
  market_error?: string | null
}

export interface AccountList {
  accounts: string[]
  source: HermesSource
}

export interface Quote {
  price: DecimalValue
  pnl: DecimalValue
  return: DecimalValue
  freshness: string
  source: string | null
  source_timestamp: string | null
  age_seconds: number | null
}

export interface EquityRow {
  at: string
  total_assets: DecimalValue
}

interface PendingTrade {
  side: 'BUY' | 'SELL'
  created_at: string
  report_id: string | null
  causes?: InvalidationCheck[]
}

interface PaperTrade {
  trade_id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  price: DecimalValue
  fee: DecimalValue
  filled_at: string
  report_id: string
}

interface InvalidationCheck {
  code: string
  status: string
  reason?: string
  evidence_id?: string
  available_at?: string
  close?: DecimalValue
  sma20?: DecimalValue
}

export interface HermesAccount {
  account_id: string
  as_of: string
  created_at: string
  current_assets: DecimalValue
  current_return: DecimalValue
  meta: {
    selection: string
    execution_gate?: string
    feed: {
      mode?: string
      auth_mode?: string | null
      execution_gate?: string
      polled_at?: string
      poll_seconds?: number
      source_update_seconds?: number | null
      error?: string | null
    }
    calendar: { next_regular_open?: string } | null
    last_open_session?: string | null
  }
  state: {
    status: string
    cash: DecimalValue
    realized_pnl: DecimalValue
    positions: Record<string, { quantity: number, entry_price: DecimalValue }>
    pending: Record<string, PendingTrade>
    trades: PaperTrade[]
    checks: Record<string, InvalidationCheck[]>
    equity: EquityRow[]
  }
  policy: {
    initial_cash: DecimalValue
    max_positions: number
    entry_fraction: DecimalValue
    round_trip_bps: DecimalValue
    policy_status: string
    proposals: string[]
  }
  coverage: { mode: string, limitations: string[] }
  metrics: {
    max_drawdown: DecimalValue
    valuation_missing_count: number
  }
  securities: Record<string, { name: string }>
  reports: HermesReport[]
  position_quotes: Record<string, Quote | undefined>
  watch_quotes: Record<string, Quote | undefined>
}

export function paperPercent(value: DecimalValue): number | null {
  const ratio = finite(value)
  return ratio == null ? null : ratio * 100
}

export function equityPoints(rows: EquityRow[], initialCash: DecimalValue) {
  // Match the ledger's high-water mark, which starts at initial cash and skips null marks.
  let peak = finite(initialCash) ?? 0
  return rows.map(row => {
    const equity = finite(row.total_assets)
    if (equity === null) return { time: row.at, equity, drawdown: null }
    peak = Math.max(peak, equity)
    const drawdown = peak > 0 ? (equity - peak) / peak * 100 : null
    return { time: row.at, equity, drawdown }
  })
}

export function matchingReports(reports: HermesReport[], query: string): HermesReport[] {
  const term = query.trim().toLowerCase()
  if (!term) return reports
  return reports.filter(report => {
    const fields = [
      report.report_id, report.key, report.author, report.published_at, report.content,
      ...report.candidates.flatMap(candidate => [candidate.name, candidate.symbol]),
    ]
    return fields.some(value => value?.toLowerCase().includes(term))
  })
}

export function selectedReport(reports: HermesReport[], key: string | null) {
  return key === null ? reports[0] : reports.find(report => report.key === key)
}
