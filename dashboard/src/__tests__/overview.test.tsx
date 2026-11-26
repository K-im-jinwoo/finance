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
