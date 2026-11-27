import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CapabilitiesProvider, PollProvider } from '../api/client'
import Overview from '../pages/Overview'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const local = { read_only: false, orders_enabled: false, telegram_delivery_enabled: false,
  routes: ['/', '/longterm', '/experiments'], phase_b_available: true } as const

it('uses the persisted experiment marks and long-term warnings without assuming missing data', async () => {
  const responses: Record<string, unknown> = {
    '/api/longterm/overview': { status: 'RUNNING', position_count: 3, pending_count: 2, risk_exit_count: 1,
      missing_close_count: 1, metrics: { total_assets: '10000000', return: '0.01' } },
    '/api/experiments': [{ experiment_id: 'E-test', cost_bps: '30', mode: 'SYNTHETIC' }],
    '/api/experiments/E-test': { experiment_id: 'E-test', cursor: 4, total_events: 5,
      state: { status: 'RUNNING', equity: [{ return: '0.10' }, { return: null }] }, metrics: { max_drawdown: '-0.042' } },
    '/api/daytrade/report': { report: null, present: false },
    '/api/status': { data_status: 'BLOCKED_NO_POINT_IN_TIME_DATASET' },
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => new Response(JSON.stringify(responses[path]))))
  render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{ ...local, routes: [...local.routes] }}><Overview /></CapabilitiesProvider>
  </PollProvider>)
  expect(screen.queryByText('Oracle 3년 검산 자료 미확보')).toBeNull()
  expect(await screen.findByText('2건')).toBeTruthy()
  expect(screen.getByText('장기 계좌 리스크 청산 대기 1건')).toBeTruthy()
  expect(screen.getByText('장기 계좌 종가 누락 1종목')).toBeTruthy()
  expect(screen.getByRole('link', { name: '계좌 보기' }).getAttribute('href')).toBe('/longterm')
  expect(await screen.findByText('-4.2%')).toBeTruthy()
  const run = screen.getByRole('heading', { name: '계산 검증 실험' }).closest('article')!
  expect(within(run).getByText('확인 불가')).toBeTruthy()
  expect(within(run).queryByText('+10%')).toBeNull()
})

it('shows stored research and swing summaries and connects to operations', async () => {
  const responses: Record<string, unknown> = {
    '/api/longterm/overview': { status: 'NO_ACCOUNT' }, '/api/experiments': [],
    '/api/daytrade/report': { present: false, report: null }, '/api/status': { data_status: 'BLOCKED_NO_POINT_IN_TIME_DATASET' },
    '/api/research/reports': { present: true, latest: { report_id: 'UR-snapshot', as_of: '2026-11-27T08:47:00+09:00', candidate_count: 9,
      decision_counts: { BUY_RECOMMENDATION: 3 } } },
    '/api/swing/reports': { present: true, latest: { report_id: 'SW-snapshot', as_of: '2026-11-26T08:47:00+09:00', candidate_count: 7 } },
    '/api/ops/overview': { present: true, verification: { data_status: 'CHECKED' } },
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(responses[path])))
  render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{ ...local, routes: [...local.routes] }}><Overview /></CapabilitiesProvider>
  </PollProvider>)
  expect(await screen.findByText('261127 장기 리서치 보고서')).toBeTruthy()
  expect(await screen.findByText('261126 테마·스윙 보고서')).toBeTruthy()
  expect(screen.queryByText('UR-snapshot')).toBeNull()
  expect(screen.getByRole('link', { name: '261127 장기 리서치 보고서' }).getAttribute('href')).toBe('/research?report=UR-snapshot')
  expect(screen.getByRole('link', { name: '261126 테마·스윙 보고서' }).getAttribute('href')).toBe('/swing?report=SW-snapshot')
  expect(screen.getByRole('link', { name: '운영 상태' }).getAttribute('href')).toBe('/ops')
})
