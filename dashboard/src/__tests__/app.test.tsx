import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App, { canonicalLocation } from '../App'

const researchFailure = vi.hoisted(() => ({ active: false }))
vi.mock('../pages/Research', async importOriginal => {
  const { default: Research } = await importOriginal<typeof import('../pages/Research')>()
  return { default: () => {
    if (researchFailure.active) throw new Error('intentional render error')
    return <Research />
  } }
})

afterEach(() => {
  cleanup()
  researchFailure.active = false
  vi.restoreAllMocks()
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
  expect(await screen.findByText('이 서버에서 읽기 전용 자료 API를 사용할 수 없습니다.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '라이트 테마로 전환' }))
  const pnlToggle = screen.getByRole('button', { name: /^KRX 적\/청/ })
  expect(pnlToggle.getAttribute('aria-pressed')).toBe('false')
  fireEvent.click(pnlToggle)
  expect(screen.getByRole('button', { name: /^글로벌 녹\/적/ }).getAttribute('aria-pressed')).toBe('true')
  await waitFor(() => expect(document.documentElement.dataset.theme).toBe('light'))
  expect(document.documentElement.dataset.pnl).toBe('global')
  expect(localStorage.getItem('finance.theme')).toBe('light')
  expect(localStorage.getItem('finance.pnl')).toBe('global')
})

it('navigates from the overview without reloading and preserves the selected experiment query', async () => {
  const id = 'E-300000000000000000000000'
  const responses: Record<string, unknown> = {
    '/api/capabilities': { read_only: false, orders_enabled: false, telegram_delivery_enabled: false,
      routes: ['/', '/hermes', '/daytrade', '/experiments', '/research', '/swing', '/ops'], phase_b_available: false },
    '/api/paper-accounts': { accounts: [], source: {} },
    '/api/daytrade/report': { present: false, report: null, sample_available: false },
    '/api/experiments': [{ experiment_id: id, cost_bps: '30', mode: 'SYNTHETIC', state: 'READY' }],
    '/api/status': { data_status: 'BLOCKED_NO_POINT_IN_TIME_DATASET' },
  }
  const api = vi.fn(async (path: string) => path in responses
    ? Response.json(responses[path]) : Response.json({ error: 'NOT_FOUND' }, { status: 404 }))
  vi.stubGlobal('fetch', api)
  const scroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
  try {
    const { container } = render(<App />)
    expect(within(container.querySelector('.global-bar')!).getByText('MULTI-TRACK')).toBeTruthy()
    const link = screen.getByRole('link', { name: '실험 보기' })
    await waitFor(() => expect(link.getAttribute('href')).toBe('/experiments?experiment=' + id))
    expect(fireEvent.click(link)).toBe(false)
    expect(await screen.findByRole('heading', { name: '계산 검증 실험', level: 1 })).toBeTruthy()
    expect(location.pathname + location.search).toBe('/experiments?experiment=' + id)
    const menus = screen.getByRole('navigation', { name: '대시보드 메뉴' })
    fireEvent.click(within(menus).getByRole('link', { name: '계산 검증 실험' }))
    expect(location.search).toBe('?experiment=' + id)
    expect(api.mock.calls.filter(([path]) => path === '/api/capabilities')).toHaveLength(1)
  } finally {
    scroll.mockRestore()
  }
})

function mockPhaseB(extra: Record<string, unknown> = {}) {
  const routes = ['/', '/hermes', '/daytrade', '/experiments', '/research', '/swing', '/ops']
  const endpoints: Record<string, unknown> = {
    '/api/capabilities': { read_only: false, orders_enabled: false, telegram_delivery_enabled: false,
      routes, phase_b_available: true },
    ...extra,
  }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(endpoints[path] ?? { present: false })))
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
}

it('contains a page render failure, allows retry and resets the boundary when navigating', async () => {
  const reportId = 'UR-fixture'
  mockPhaseB({
    '/api/research/reports': { present: true, latest: { report_id: reportId }, reports: [{ report_id: reportId }] },
    ['/api/research/reports/' + reportId]: { present: false, reason: 'REPORT_NOT_FOUND' },
  })
  window.history.replaceState(null, '', '/research')
  researchFailure.active = true
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const { container } = render(<App />)
  const main = screen.getByRole('main')
  const navigation = screen.getByRole('navigation', { name: '대시보드 메뉴' })
  const fallback = within(main).getByRole('alert')
  expect(fallback.textContent).toContain('화면을 표시하는 중 오류가 발생했습니다.')
  expect(fallback.textContent).not.toContain('intentional render error')
  expect(screen.getByRole('button', { name: '수동 새로고침' })).toBeTruthy()
  expect(container.querySelector('.global-bar')).toBeTruthy()

  researchFailure.active = false
  fireEvent.click(within(main).getByRole('button', { name: '다시 시도' }))
  expect(await within(main).findByRole('heading', { name: '통합 리서치 & 차트', level: 1 })).toBeTruthy()
  expect(within(main).queryByRole('alert')).toBeNull()

  const navigate = (name: string) => fireEvent.click(within(navigation).getByRole('link', { name }))
  navigate('운영·포트폴리오')
  researchFailure.active = true
  navigate('통합 리서치 & 차트')
  expect(await within(main).findByRole('alert')).toBeTruthy()

  navigate('운영·포트폴리오')
  expect(location.pathname).toBe('/ops')
  expect(await within(main).findByRole('heading', { name: '운영·포트폴리오', level: 1 })).toBeTruthy()
  expect(within(main).queryByRole('alert')).toBeNull()
})

it('labels aggregated evidence and keeps unknown provenance on a single swing report', async () => {
  const reportId = 'SW-fixture'
  mockPhaseB({
    '/api/swing/reports': { present: true, latest: { report_id: reportId }, reports: [{ report_id: reportId }] },
    ['/api/swing/reports/' + reportId]: { present: true, report: { report_id: reportId } },
  })
  window.history.replaceState(null, '', '/ops')
  const { container } = render(<App />)
  const header = within(container.querySelector('.global-bar')!)
  expect(header.getByText('MULTI-TRACK')).toBeTruthy()
  const navigation = await screen.findByRole('link', { name: '테마·스윙 & 성과' })
  fireEvent.click(navigation)
  await screen.findByText(reportId, { selector: 'span' })
  expect(header.getByText('UNKNOWN')).toBeTruthy()
  const tabs = screen.getByRole('navigation', { name: '스윙·성과 보기' })
  fireEvent.click(within(tabs).getByRole('link', { name: '사후 성과 트래커' }))
  expect(header.getByText('MULTI-TRACK')).toBeTruthy()
  fireEvent.click(within(tabs).getByRole('link', { name: '스윙 보고서' }))
  expect(header.getByText('UNKNOWN')).toBeTruthy()
})
