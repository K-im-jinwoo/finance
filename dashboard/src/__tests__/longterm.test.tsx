import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../App'

const id = 'UR-aaaaaaaaaaaaaaaaaaaaaaaa'
const account = {
  status: 'RUNNING', policy: { max_positions: 5, round_trip_bps: 30 },
  updated_at: '2026-11-13T09:30:00Z', latest_report: { mode: 'SYNTHETIC' },
  risk_exit_count: 1, missing_close_count: 1,
  metrics: { total_assets: null, return: null, unrealized_pnl: null, realized_pnl: '0', cash: '8000000', max_drawdown: '-0.02' },
  positions: [{ symbol: '005930', name: '합성 반도체', entry_report_id: id, entry_date: '2026-11-12',
    entry_price: '100000', quantity: 19, decision: 'VERIFY_PENDING', review_status: 'REVIEW_REQUIRED', close_missing: true }],
  pending: [{ id: 1, name: '합성 반도체', symbol: '005930', side: 'SELL', status: 'PENDING', reason: 'EXCLUDED',
    signal_reason: 'EXCLUDED', signal_date: '2026-11-13', report_id: id }],
  orders: [], fills: [], equity: [{ date: '2026-11-13', total_assets: null, drawdown: null }], reports: [],
}

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function serve(readOnly: boolean) {
  const fetch = vi.fn(async (path: string, init?: RequestInit) => Response.json(
    path === '/api/capabilities'
      ? { read_only: readOnly, routes: ['/', '/longterm', '/daytrade', '/experiments', '/research', '/swing', '/ops'], phase_b_available: true }
      : path === '/api/longterm/account' || init?.method === 'POST' ? account : { present: false },
  ))
  vi.stubGlobal('fetch', fetch)
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  return fetch
}

it('redirects Hermes and hides local actions and experiments on the integrated read-only server', async () => {
  const api = serve(true)
  window.history.replaceState(null, '', '/hermes')
  render(<App />)
  expect(await screen.findByRole('heading', { name: '장기 모의계좌', level: 1 })).toBeTruthy()
  expect(await screen.findByText('수수료 0.30%')).toBeTruthy()
  expect(screen.queryByText('읽기 전용', { selector: '.badge' })).toBeNull()
  expect(location.pathname).toBe('/longterm')
  expect(within(screen.getByRole('navigation', { name: '대시보드 메뉴' })).getAllByRole('link')).toHaveLength(6)
  expect(screen.queryByRole('button', { name: '일시중지' })).toBeNull()
  expect(screen.queryByRole('button', { name: '원장 갱신' })).toBeNull()
  expect(api.mock.calls.some(([path]) => path.startsWith('/api/experiments'))).toBe(false)
})

it('keeps the integrated home off the local experiment and status APIs from first load', async () => {
  const api = serve(true)
  render(<App />)
  await screen.findByRole('heading', { name: '통합 홈', level: 1 })
  expect(screen.queryByText('READ_ONLY')).toBeNull()
  expect(screen.queryByRole('heading', { name: '계산 검증 실험' })).toBeNull()
  expect(api.mock.calls.some(([path]) => path === '/api/status' || path.startsWith('/api/experiments'))).toBe(false)
})

it('shows risk and missing-price context, links the entry report chart, and posts only explicit controls', async () => {
  const api = serve(false)
  window.history.replaceState(null, '', '/longterm')
  render(<App />)
  expect(await screen.findByText('검토 필요')).toBeTruthy()
  expect(screen.getByText('종가 누락 1종목')).toBeTruthy()
  expect(screen.getByText('리스크 청산 대기 1건')).toBeTruthy()
  const link = within(screen.getByRole('table', { name: '보유 종목' })).getByRole('link', { name: /합성 반도체/ })
  expect(link.getAttribute('href')).toBe('/research?report=' + id + '&symbol=005930')
  fireEvent.click(await screen.findByRole('button', { name: '일시중지' }))
  await waitFor(() => expect(api.mock.calls.some(([path, init]) => path === '/api/longterm/account/pause' && init?.method === 'POST')).toBe(true))
})
