import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App, { canonicalLocation } from '../App'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
  document.documentElement.dataset.theme = 'dark'
  document.documentElement.dataset.pnl = 'krx'
  window.history.replaceState(null, '', '/')
})

it('canonicalizes legacy links without changing the query or fragment', () => {
  const query = '?experiment=E-test&event=A%2B1&trade=t&report=r&x=x&x=y'
  expect(canonicalLocation(new URL('http://127.0.0.1/' + query + '#anchor'))).toBe('/experiments' + query + '#anchor')
  expect(canonicalLocation(new URL('http://127.0.0.1/hermes?account=abc'))).toBe('/hermes?account=abc')
})

it('hides other tracks and writes no paper action when the server is read-only', async () => {
  window.history.replaceState(null, '', '/daytrade')
  const api = vi.fn(async (path: string) => new Response(JSON.stringify(path === '/api/capabilities'
    ? { read_only: true, orders_enabled: false, telegram_delivery_enabled: false, routes: ['/daytrade'], phase_b_available: false }
    : { present: false, report: null, sample_available: true })))
  vi.stubGlobal('fetch', api)
  render(<App />)
  expect(await screen.findByText('READ_ONLY')).toBeTruthy()
  expect(screen.queryByRole('link', { name: '통합 홈' })).toBeNull()
  expect(screen.queryByRole('link', { name: '계산 검증 실험' })).toBeNull()
  expect(screen.queryByRole('button', { name: '오프라인 샘플 실행' })).toBeNull()
  expect(api.mock.calls.map(([path]) => path).sort()).toEqual(['/api/capabilities', '/api/daytrade/report'])
})

it('persists theme and market color settings independently', async () => {
  window.history.replaceState(null, '', '/research')
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    read_only: false, orders_enabled: false, telegram_delivery_enabled: false, routes: ['/research'], phase_b_available: false,
  }))))
  render(<App />)
  expect(await screen.findByText('Phase B 준비 중')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '라이트 테마로 전환' }))
  fireEvent.click(screen.getByRole('button', { name: '글로벌 녹색 상승·적색 하락 적용' }))
  await waitFor(() => expect(document.documentElement.dataset.theme).toBe('light'))
  expect(document.documentElement.dataset.pnl).toBe('global')
  expect(localStorage.getItem('finance.theme')).toBe('light')
  expect(localStorage.getItem('finance.pnl')).toBe('global')
})
