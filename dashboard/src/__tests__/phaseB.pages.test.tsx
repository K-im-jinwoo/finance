import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { PollProvider } from '../api/client'
import Ops from '../pages/Ops'
import Swing from '../pages/Swing'

const swingId = 'SW-cccccccccccccccccccccccc'
const regularId = 'R-20261113T0900Z-AB12CD34'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

function useApi(responses: Record<string, unknown>) {
  const api = vi.fn(async (path: string) => Response.json(responses[path] ?? { present: false }))
  vi.stubGlobal('fetch', api)
  return api
}

it('filters saved swing evidence and selects a report and horizon in the performance tracker', async () => {
  window.history.replaceState(null, '', '/swing')
  const summaries = [{ report_id: swingId, price_session: '2026-11-13', candidate_count: 2 }]
  const results = [{
    report_id: regularId, symbol: '900101', trading_days: 20, status: 'COMPLETE',
    gross_return: '0.041', net_return: '0.038', round_trip_cost_bps: '30',
  }, { report_id: swingId, symbol: '900103', trading_days: 60, status: 'PENDING_ENTRY' }]
  const api = useApi({
    '/api/swing/reports': { present: true, reports: summaries, latest: summaries[0] },
    ['/api/swing/reports/' + swingId]: { present: true, report: {
      report_id: swingId, mode: 'SYNTHETIC', candidates: [
        { symbol: '900101', name: '합성 장비', hermes_duration: 'POTENTIALLY_PERSISTENT', hermes_evidence_index: 0,
          news_evidence: [{ category: 'CONTRACT', title: '합성 기사', url: 'https://example.invalid/evidence' }] },
        { symbol: '900102', name: '합성 소재',
          news_evidence: [{ category: 'EARNINGS', title: '저장된 실적', url: 'javascript:alert(1)' }] },
      ],
    } },
    '/api/performance': { present: true, records: results, summary: [] },
    ['/api/performance?report_id=' + regularId]: { present: true, records: results.slice(0, 1), summary: [{
      ruleset_version: 'AUDIT', decision: 'WATCH', strategy: 'MOMENTUM', trading_days: 20,
      sample_count: 1, win_rate: '1', average_net_return: '0.038',
    }] },
  })
  render(<PollProvider defaultInterval={0}><Swing /></PollProvider>)
  expect(await screen.findByText('지속성 지속 가능')).toBeTruthy()
  expect(screen.getByText('지속성 확인 필요')).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: '촉매 분류' }), { target: { value: 'CONTRACT' } })
  expect(screen.getByRole('link', { name: '합성 기사' }).getAttribute('href')).toBe('https://example.invalid/evidence')
  expect(screen.queryByRole('heading', { name: /합성 소재/ })).toBeNull()
  const tracker = screen.getByRole('link', { name: '사후 성과 트래커' })
  fireEvent.click(tracker)
  expect(tracker.getAttribute('aria-current')).toBe('page')
  expect(window.location.search).toContain('view=performance')
  const all = await screen.findByRole('table', { name: '거래일별 사후 성과' })
  expect(within(all).getByText('진입가 확인 대기')).toBeTruthy()
  fireEvent.change(screen.getByRole('combobox', { name: '보고서' }), { target: { value: regularId } })
  fireEvent.change(screen.getByRole('combobox', { name: '평가 기간' }), { target: { value: '20' } })
  const aggregate = await screen.findByRole('table', { name: '사후 성과 집계' })
  expect(within(aggregate).getByText('100%')).toBeTruthy()
  expect(within(screen.getByRole('table', { name: '거래일별 사후 성과' })).getByText('+4.1%')).toBeTruthy()
  expect(api.mock.calls.map(([path]) => path)).toContain('/api/performance?report_id=' + regularId)
  fireEvent.change(screen.getByRole('combobox', { name: '평가 기간' }), { target: { value: '5' } })
  expect(screen.getByText('이 보고서와 기간에 저장된 성과 기록이 없습니다.')).toBeTruthy()
})

it('renders native news gauges, aggregated holdings, recorded validation and daemon state', async () => {
  useApi({ '/api/ops/overview': {
    present: true, holdings: { present: true, positions: [
      { symbol: '900101', quantity: '13', average_price: '50700', brokers: ['합성 브로커 A', '합성 브로커 B'] },
    ] },
    news_budget: { present: true, day: '2026-11-26', monthly_used: 12_340, monthly_limit: 50_000,
      daily_used: 640, daily_limit: 2000, provider_halted: true, policy_status: 'UNKNOWN',
      cooldowns: { SWING: { status: 'COOLDOWN' } } },
    verification: { present: true, mode: 'SYNTHETIC', oracle_inventory: { tables: { financial_snapshots: { symbols: 5 } } },
      actual_price_validation: { results: [{ symbol: '900101', start: '2026-07-01', end: '2026-11-13',
        calculation_dates_checked: 60, source_features_equal_independent_formulas: true }] } },
    daytrade: { present: true, status: 'OBSERVING', ws_frames: 2000, active_sockets: 1, entry_blocked: true,
      live_settings: { max_daily_entries: 3, strategy_config: { take_profit_bps: '60' } } },
  } })
  const view = render(<PollProvider defaultInterval={0}><Ops /></PollProvider>)
  expect(within(await screen.findByRole('table', { name: '브로커 합산 보유' })).getByText('13주')).toBeTruthy()
  const gauges = screen.getAllByRole('progressbar') as HTMLProgressElement[]
  expect(gauges.map(gauge => [gauge.value, gauge.max])).toEqual([[640, 2000], [12_340, 50_000]])
  expect(screen.getByText('제공자 차단 있음')).toBeTruthy()
  expect(screen.queryByText('무료 정책 확인 불가')).toBeNull()
  expect(screen.getByText('쿨다운 중')).toBeTruthy()
  expect(within(screen.getByRole('table', { name: '보관 가격 검산' })).getByText('일치')).toBeTruthy()
  expect(screen.getByText('장중 모니터링')).toBeTruthy()
  expect(screen.queryByText('OBSERVING')).toBeNull()
  expect(screen.queryByText('strategy_config.take_profit_bps')).toBeNull()
  expect(view.container.querySelector('main h2, .page-content h2')?.textContent).toBe('단타 자동 수집기')
  expect(view.container.querySelector('.system-details')?.hasAttribute('open')).toBe(false)
  expect(view.container.querySelector('[style]')).toBeNull()
})

it('shows a local source absence without inventing holdings or budget values', async () => {
  useApi({ '/api/ops/overview': { present: false, holdings: { present: false, reason: 'DATABASE_MISSING' } } })
  render(<PollProvider defaultInterval={0}><Ops /></PollProvider>)
  await waitFor(() => expect(screen.getByText('연결할 로컬 데이터베이스 파일이 없습니다.')).toBeTruthy())
  expect(screen.queryByRole('progressbar')).toBeNull()
  expect(screen.queryByRole('table', { name: '브로커 합산 보유' })).toBeNull()
})

it('keeps confirmed limits, unavailable usage and a genuinely empty holdings list distinct', async () => {
  useApi({ '/api/ops/overview': {
    present: true,
    holdings: { present: true, positions: [] },
    news_budget: { present: false, daily_limit: 2000, monthly_limit: 50000 },
    daytrade: { present: true, status: 'OBSERVING' },
    verification: { present: true, data_status: 'THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY',
      checked_at_kst: '2026-09-30T12:23:35+09:00' },
  } })
  render(<PollProvider defaultInterval={0}><Ops /></PollProvider>)
  expect(await screen.findByText('신규 진입 여부 확인 불가')).toBeTruthy()
  expect(screen.getByText('등록된 보유 종목 0개')).toBeTruthy()
  expect(screen.getByText('사용량 확인 불가')).toBeTruthy()
  expect(screen.getByText('2,000회')).toBeTruthy()
  expect(screen.getByText('50,000회')).toBeTruthy()
  expect(screen.getByText('3년 백테스트 자료 일부 부족')).toBeTruthy()
  expect(screen.getByText('9월 30일')).toBeTruthy()
  expect(screen.queryByRole('progressbar')).toBeNull()
})
