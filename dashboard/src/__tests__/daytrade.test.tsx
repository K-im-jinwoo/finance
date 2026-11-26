import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CapabilitiesProvider, PollProvider, useRefresh } from '../api/client'
import Daytrade from '../pages/Daytrade'
import { equityPoints } from '../pages/daytradeData'
import type { DaytradeData, DaytradeReport } from '../pages/daytradeData'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

const at = (second: number) => '2026-11-25T09:00:' + String(second).padStart(2, '0') + '+09:00'
const report: DaytradeReport = {
  profile: 'PAPER_FIXTURE', dataset_kind: 'SYNTHETIC', mode: 'PAPER', paper_ready: false,
  account: {
    initial_capital: 10_000_000, cash: 10_000_000, final_equity: null, valuation_status: 'MARK_UNCERTAIN',
    net_pnl: null, realized_pnl_net: -1234, return_pct: null, completed_trades: 1, win_rate_pct: '0',
    mdd_pct: null, mdd_observed_pct: '0.1', mdd_uncertain: true,
  },
  sessions: { '2026-11-25': 'UNFLATTENED' },
  positions: { '000660': { quantity: 1, cost: 12345 } },
  completed_trades: [{
    symbol: '005930', entry_at: at(1), exit_at: at(2), buy_quantity: 1,
    average_buy_price: 50000, average_sell_price: 49000, fees: 200, tax: 34, realized_pnl: -1234,
  }],
  fills: [],
  equity_curve: [{ at: at(1), equity: 10_000_000, phase: 'OPEN' }, { at: at(2), equity: null, phase: 'LOSS_LOCKED', locks: ['LOSS_LOCKED'] }],
  scan_history: [{ at: at(1), selected: [{ symbol: '005930', turnover: 558_099_999_999, spread_bps: '7.7' }],
    rejected: [{ symbol: '000660', reason: 'WIDE_SPREAD' }] }],
  signals: Array.from({ length: 32 }, (_, index) => ({ at: at(index), symbol: '005930', action: 'HOLD', score: index })),
  risk_events: Array.from({ length: 17 }, (_, index) => ({ at: at(index), phase: 'LOSS_LOCKED', locks: ['LOSS_LOCKED'] })),
  quality_events: [], decisions: [], current_risk: { phase: 'LOSS_LOCKED', locks: ['LOSS_LOCKED'], entry_allowed: false },
  symbol_names: { '005930': '삼성전자', '000660': 'SK하이닉스' },
}
function Refresh() {
  const refresh = useRefresh()
  return <button onClick={refresh}>새 자료</button>
}

function setup(readOnly: boolean, initial: DaytradeData, next = initial) {
  const api = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(initial)))
    .mockImplementation(async () => new Response(JSON.stringify(next)))
  vi.stubGlobal('fetch', api)
  render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{ read_only: readOnly, orders_enabled: false,
      telegram_delivery_enabled: false, phase_b_available: false, routes: ['/daytrade'] }}>
      <Refresh /><Daytrade />
    </CapabilitiesProvider>
  </PollProvider>)
  return api
}

async function open(summary: HTMLElement) {
  const details = summary.closest('details')!
  details.open = true
  await act(async () => fireEvent(details, new Event('toggle')))
  return details
}

it('renders actual API-shaped values and keeps lazy expansions across refreshes', async () => {
  const next = { ...report, scan_history: [...report.scan_history, {
    at: at(31), selected: [], rejected: [{ symbol: '005930', reason: 'WIDE_SPREAD' }],
  }] }
  setup(true, { present: true, sample_available: true, report }, { present: true, sample_available: true, report: next })
  expect((await screen.findByRole('table', { name: '미청산 보유 종목' })).textContent).toContain('12,345원')
  expect(screen.getByRole('table', { name: '완결 거래 원장' }).textContent).toContain('-1,234원')
  expect(screen.getByText('5,580억 원')).toBeTruthy()
  expect(screen.getByText('0.08% (7.7bps)')).toBeTruthy()
  const signalRegion = screen.getByRole('region', { name: '매수·매도·대기 신호' })
  const riskRegion = screen.getByRole('region', { name: '위험·데이터 품질' })
  expect(within(signalRegion).getAllByRole('listitem')).toHaveLength(30)
  expect(within(riskRegion).getAllByRole('listitem')).toHaveLength(15)
  expect(screen.queryByRole('table', { name: '감시 종목 제외' })).toBeNull()
  const details = await open(screen.getByText('제외 종목 상세 보기 (1종목)'))
  expect(within(details).getByRole('table').textContent).toContain('SK하이닉스')
  fireEvent.click(screen.getByRole('button', { name: '새 자료' }))
  await waitFor(() => expect(within(details).getByRole('table').textContent).toContain('삼성전자'))
  expect(details.open).toBe(true)
  expect(screen.queryByRole('button', { name: '오프라인 샘플 실행' })).toBeNull()
})

it('exposes the sample action only with writable capabilities and sends the local POST contract', async () => {
  const api = setup(false, { present: false, sample_available: true, report: null })
  fireEvent.click(await screen.findByRole('button', { name: '오프라인 샘플 실행' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/daytrade/simulate-sample', expect.objectContaining({
    method: 'POST', body: '{}', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Paper-Action': '1' },
  })))
})

it('preserves missing valuations and treats diagnostic truncation as a chart gap', () => {
  const marks = [...report.equity_curve, {
    at: at(3), phase: 'OPEN', equity: 50_000_000, diagnostic_truncation: true,
  }, { at: at(4), phase: 'OPEN', equity: 9_900_000 }]
  expect(equityPoints(marks, report.account.initial_capital).map(point => [point.equity, point.drawdown, point.risk])).toEqual([
    [10_000_000, 0, null], [null, null, '일손실 한도 잠금'], [null, null, null], [9_900_000, -1, null],
  ])
})

it('selects dated ledgers without write controls on the read-only server', async () => {
  window.history.replaceState(null, '', '/daytrade?date=2026-11-24')
  const api = vi.fn(async (path: string) => Response.json(path === '/api/daytrade/sessions'
    ? { present: true, sessions: ['2026-11-25', '2026-11-24'] }
    : { present: false, report: null, sample_available: true }))
  vi.stubGlobal('fetch', api)
  render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{ read_only: true, orders_enabled: false, telegram_delivery_enabled: false,
      phase_b_available: false, routes: ['/daytrade'], ...{ daytrade_sessions_available: true } }}>
      <Daytrade />
    </CapabilitiesProvider>
  </PollProvider>)
  const selector = await screen.findByRole('combobox', { name: '거래일' }) as HTMLSelectElement
  expect(selector.value).toBe('2026-11-24')
  expect(api.mock.calls.map(([path]) => path)).toContain('/api/daytrade/report?date=2026-11-24')
  fireEvent.change(selector, { target: { value: '2026-11-25' } })
  await waitFor(() => expect(api.mock.calls.map(([path]) => path)).toContain('/api/daytrade/report?date=2026-11-25'))
  expect(window.location.search).toBe('?date=2026-11-25')
  expect(screen.queryByRole('button', { name: '오프라인 샘플 실행' })).toBeNull()
})


it('loads cumulative realized amounts on demand without counting unavailable sessions', async () => {
  const api = vi.fn(async (path: string) => Response.json(path === '/api/daytrade/sessions'
    ? { present: true, sessions: ['2026-11-25', '2026-11-24'] }
    : path.endsWith('date=2026-11-24')
      ? { present: true, report, sample_available: false }
      : { present: false, report: null, sample_available: false }))
  vi.stubGlobal('fetch', api)
  render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{ read_only: true, orders_enabled: false, telegram_delivery_enabled: false,
      phase_b_available: false, routes: ['/daytrade'], daytrade_sessions_available: true }}>
      <Daytrade />
    </CapabilitiesProvider>
  </PollProvider>)
  const load = await screen.findByRole('button', { name: '최근 2개 원장 집계' })
  expect(api.mock.calls.map(([path]) => path)).not.toContain('/api/daytrade/report?date=2026-11-24')
  fireEvent.click(load)
  const table = await screen.findByRole('table', { name: '일자별 단타 성과' })
  expect(within(table).getByText('-1,234원')).toBeTruthy()
  expect(within(table).getByText('원장 확인 불가')).toBeTruthy()
  expect(screen.getByText('2개 중 1개 원장에서 실현손익을 확인했습니다.')).toBeTruthy()
})
