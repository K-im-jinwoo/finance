import type { Presence } from '../api/phaseB'
import type { VerificationStatus } from './experimentModel'

export type Quantity = string | number | null
type RawHolding = { broker: string; symbol: string; quantity: Quantity; average_price: Quantity; acquired_on?: string | null }
type Position = { symbol: string; quantity: Quantity; average_price: Quantity; brokers: string[] }
type Thesis = {
  thesis_id: string; symbol: string; status: string; created_at?: string
  facts: string[]; assumptions: string[]; invalidation_conditions: string[]; additional_check_conditions: string[]
}
type Review = {
  symbol: string
  present?: boolean
  reason?: string
  current_price?: Quantity
  return_rate?: Quantity
  thesis_status?: string
  actions?: string[]
  reasons?: string[]
  additional_checks?: string[]
  price_date?: string
}
export type Holdings = Presence & { items?: RawHolding[]; positions?: Position[]; theses?: Thesis[]; reviews?: Review[] }
type Cooldown = { status?: string; last_reserved_at?: string | null; available_at?: string | null }
export type NewsBudget = Presence & {
  day?: string; month?: string
  daily_used?: number | null; daily_limit?: number | null
  monthly_used?: number | null; monthly_limit?: number | null
  policy_status?: string | null; provider_halted?: boolean | null
  day_blocked?: boolean | null; month_blocked?: boolean | null
  blocked_today?: boolean | null; blocked_month?: boolean | null
  cooldowns?: Record<string, Cooldown>
}
type PublicSetting = string | number | boolean | null
export type PublicSettings = Record<string, PublicSetting | Record<string, PublicSetting>>

export type DaemonSummary = Presence & {
  status?: string | null; at?: string | null; orders_enabled?: false; active_sockets?: number; ws_frames?: number
  entry_blocked?: boolean; ledger_lock?: string | boolean; error_code?: string | null; unclosed_positions?: number
  notification_status?: string | null; token_status?: string | null; token_valid_until?: string | null; initial_capital?: Quantity
  live_settings?: PublicSettings
}
export type OpsVerification = VerificationStatus & {
  present?: boolean; reason?: string; data_status?: string; checked_at_kst?: string; mode?: 'SYNTHETIC'
}
export type OpsData = Presence & {
  holdings?: Holdings
  news_budget?: NewsBudget
  daytrade?: DaemonSummary
  verification?: OpsVerification
}
