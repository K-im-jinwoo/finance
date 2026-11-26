import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { PollProvider } from '../api/client'
import Overview from '../pages/Overview'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('uses the persisted experiment marks and Hermes counters without assuming missing data', async () => {
  const responses: Record<string, unknown> = {
    '/api/paper-accounts': { accounts: ['P-test'], source: { state: 'CONNECTED' } },
    '/api/paper-accounts/P-test': { coverage: { mode: 'OBSERVED' }, metrics: { pending_trades: 2 },
      current_assets: '10000000', current_return: '0.01', reports: [], position_quotes: {}, watch_quotes: {} },
    '/api/experiments': [{ experiment_id: 'E-test', cost_bps: '30', mode: 'SYNTHETIC' }],
    '/api/experiments/E-test': { experiment_id: 'E-test', cursor: 4, total_events: 5,
      state: { status: 'RUNNING', equity: [{ return: '0.10' }, { return: null }] }, metrics: { max_drawdown: '-0.042' } },
    '/api/daytrade/report': { report: null, present: false },
    '/api/status': { data_status: 'BLOCKED_NO_POINT_IN_TIME_DATASET' },
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(responses[path]))))
  render(<PollProvider defaultInterval={0}><Overview /></PollProvider>)
  expect(await screen.findByText('Oracle 3년 검산 자료 미확보')).toBeTruthy()
  expect(await screen.findByText('2건')).toBeTruthy()
  expect(await screen.findByText('-4.2%')).toBeTruthy()
  const run = screen.getByRole('heading', { name: '계산 검증 실험' }).closest('article')!
  expect(within(run).getByText('확인 불가')).toBeTruthy()
  expect(within(run).queryByText('+10%')).toBeNull()
})

it('shows stored research and swing summaries and connects to operations', async () => {
  const responses: Record<string, unknown> = {
    '/api/paper-accounts': { accounts: [], source: {} }, '/api/experiments': [],
    '/api/daytrade/report': { present: false, report: null }, '/api/status': { data_status: 'BLOCKED_NO_POINT_IN_TIME_DATASET' },
    '/api/research/reports': { present: true, latest: { report_id: 'UR-snapshot', candidate_count: 9,
      decision_counts: { BUY_RECOMMENDATION: 3 } } },
    '/api/swing/reports': { present: true, latest: { report_id: 'SW-snapshot', candidate_count: 7 } },
    '/api/ops/overview': { present: true, verification: { data_status: 'CHECKED' } },
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(responses[path])))
  render(<PollProvider defaultInterval={0}><Overview /></PollProvider>)
  expect(await screen.findByText('UR-snapshot')).toBeTruthy()
  expect(await screen.findByText('SW-snapshot')).toBeTruthy()
  expect(screen.getByRole('link', { name: '리서치 보고서' }).getAttribute('href')).toBe('/research?report=UR-snapshot')
  expect(screen.getByRole('link', { name: '스윙 보고서' }).getAttribute('href')).toBe('/swing?report=SW-snapshot')
  expect(screen.getByRole('link', { name: '운영 상태' }).getAttribute('href')).toBe('/ops')
})
