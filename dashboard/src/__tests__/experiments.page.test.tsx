import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CapabilitiesProvider, PollProvider } from '../api/client'
import Experiments from '../pages/Experiments'
import type { Experiment, ExperimentSummary } from '../pages/experimentModel'

const exp10 = 'E-100000000000000000000000'
const exp30 = 'E-300000000000000000000000'
const exp60 = 'E-600000000000000000000000'
const unrelated = 'E-aaaaaaaaaaaaaaaaaaaaaaaa'
const at = '2026-04-13T20:00:00+09:00'
const nextDay = '2026-04-14T20:00:00+09:00'

function fixture(id: string, cost: string): Experiment {
  return {
    experiment_id: id, cursor: 2, total_events: 5,
    coverage: { dataset_id: 'same-input', mode: 'SYNTHETIC', supported_start: at, supported_end: nextDay },
    policy: { initial_cash: '10000000', round_trip_bps: cost },
    state: {
      status: 'READY', clock: at, cash: '10000000', positions: {}, checks: {}, invalidated: {},
      pending: {}, observations: [], outbox: [], trades: [], reports: [],
      equity: [{ at, total_assets: '10100000', return: '0.01' }],
    },
    metrics: {
      max_drawdown: '0', total_cost: '1000', realized_pnl: '0', cash_ratio: '0.8',
      completed_round_trips: 0, open_positions: 0, win_rate: null, valuation_missing_count: 0,
    },
  }
}

const summary = (experiment: Experiment): ExperimentSummary => ({
  experiment_id: experiment.experiment_id,
  state: experiment.state.status,
  cursor: experiment.cursor,
  cost_bps: experiment.policy.round_trip_bps,
  dataset_id: experiment.coverage.dataset_id,
  mode: experiment.coverage.mode,
})

function mockApi(experiments: Experiment[], action?: (path: string, init: RequestInit) => Response, status: unknown = {}) {
  const fetchMock = vi.fn(async (request: string | URL | Request, init?: RequestInit) => {
    const path = String(request)
    if (init?.method === 'POST' && action) return action(path, init)
    if (path === '/api/experiments') return Response.json(experiments.map(summary))
    if (path === '/api/status') return Response.json(status)
    if (path.endsWith('/drafts')) return Response.json([])
    const found = experiments.find(experiment => path === '/api/experiments/' + experiment.experiment_id)
    if (found) return Response.json(found)
    return Response.json({ error: 'NOT_FOUND' }, { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function open(search: string) {
  window.history.replaceState(null, '', '/experiments?' + search)
  return render(<PollProvider defaultInterval={0}>
    <CapabilitiesProvider value={{
      read_only: false, orders_enabled: false, telegram_delivery_enabled: false,
      routes: ['/experiments'], phase_b_available: false,
    }}>
      <Experiments />
    </CapabilitiesProvider>
  </PollProvider>)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

it('rejects invalid detail combinations without displaying evidence and preserves unrelated URL state when closed', async () => {
  const experiment = fixture(exp30, '30')
  experiment.state.reports = [{ report_id: 'r1', at, candidates: [] }, { report_id: 'r2', at, candidates: [] }]
  experiment.state.outbox = [{ event_id: 'evt', at, kind: 'BUY', detail: { report_id: 'r1' } }]
  mockApi([experiment])
  open(new URLSearchParams({ experiment: exp30, event: 'evt', report: 'r2', source: 'draft' }).toString())

  const drawer = await screen.findByRole('dialog', { name: '선택한 상세 근거' })
  await within(drawer).findByText(/해당 실험에 상세 기록이 없습니다/)
  expect(within(drawer).queryByText('r2')).toBeNull()
  fireEvent.click(within(drawer).getByRole('button', { name: '상세 닫기' }))
  expect(new URLSearchParams(location.search)).toEqual(new URLSearchParams({ experiment: exp30, source: 'draft' }))
})

it('overlays all three costs for the same input and preserves missing latest valuations', async () => {
  const low = fixture(exp10, '10')
  low.state.equity.push({ at: nextDay, total_assets: null, return: null })
  const other = fixture(unrelated, '10')
  other.coverage = { ...other.coverage, dataset_id: 'unrelated-input', mode: 'OBSERVED' }
  const fetchMock = mockApi([other, low, fixture(exp30, '30'), fixture(exp60, '60')])
  const { container } = open('experiment=' + exp30 + '&view=costs')

  const table = await screen.findByRole('table', { name: '비용별 핵심 지표' })
  await within(table).findAllByText('+1.00%')
  const lowRow = within(table).getByRole('rowheader', { name: '10bps' }).closest('tr')!
  expect(within(lowRow).getAllByText('확인 불가')).toHaveLength(3)
  expect(container.querySelector('.chart-cost-10')).not.toBeNull()
  expect(container.querySelector('.chart-cost-30')).not.toBeNull()
  expect(container.querySelector('.chart-cost-60')).not.toBeNull()
  expect(fetchMock.mock.calls.map(([path]) => path)).not.toContain('/api/experiments/' + unrelated)
})

it('submits the paper action contract and reflects the accepted control before the next read', async () => {
  const ready = fixture(exp30, '30')
  const running = { ...ready, state: { ...ready.state, status: 'RUNNING' } }
  const action = vi.fn((_path: string, _init: RequestInit) => Response.json(running))
  mockApi([ready], action)
  open('experiment=' + exp30)
  const start = await screen.findByRole('button', { name: '실험 시작' })
  fireEvent.click(start)

  await screen.findByText('계산 중', { selector: 'span' })
  expect(action).toHaveBeenCalledWith('/api/experiments/' + exp30 + '/start', expect.objectContaining({
    method: 'POST', body: '{}', credentials: 'same-origin', headers: expect.objectContaining({ 'X-Paper-Action': '1' }),
  }))
  expect((screen.getByRole('button', { name: '일시중지' }) as HTMLButtonElement).disabled).toBe(false)
})

it('keeps a missing historical selection without fetching the default experiment', async () => {
  const fetchMock = mockApi([fixture(exp30, '30')])
  open('experiment=' + unrelated + '&report=old')

  await screen.findByText(/선택한 실험을 찾을 수 없습니다/)
  expect(new URLSearchParams(location.search).get('experiment')).toBe(unrelated)
  expect(fetchMock.mock.calls.map(([path]) => path)).not.toContain('/api/experiments/' + exp30)
})

it('creates an experiment with the selected fee assumption and selects it in the URL', async () => {
  const experiments = [fixture(exp30, '30')]
  const action = vi.fn((_path: string, _init: RequestInit) => {
    experiments.unshift(fixture(exp60, '60'))
    return Response.json({ experiment_id: exp60 }, { status: 201 })
  })
  mockApi(experiments, action)
  open('experiment=' + exp30)
  fireEvent.change(screen.getByLabelText('새 실험 왕복 비용'), { target: { value: '60' } })
  const create = screen.getByRole('button', { name: '별도 검증 실험 생성' })
  expect(create.classList.contains('primary-button')).toBe(true)
  fireEvent.click(create)

  await screen.findByText(/가상 검증 · 왕복 60bps/)
  expect(new URLSearchParams(location.search).get('experiment')).toBe(exp60)
  expect(action).toHaveBeenCalledWith('/api/experiments', expect.objectContaining({ method: 'POST', body: '{"cost_bps":"60"}' }))
})

it('marks the active experiment view and updates the query when switching views', async () => {
  mockApi([fixture(exp30, '30')])
  open('experiment=' + exp30)
  const tabs = screen.getByRole('navigation', { name: '검증 실험 보기' })
  expect(tabs.classList.contains('experiment-tabs')).toBe(true)
  expect(within(tabs).getByRole('link', { name: '단일 실험' }).getAttribute('aria-current')).toBe('page')
  fireEvent.click(within(tabs).getByRole('link', { name: '비용 민감도 비교' }))
  expect(within(tabs).getByRole('link', { name: '비용 민감도 비교' }).getAttribute('aria-current')).toBe('page')
  expect(within(tabs).getByRole('link', { name: '단일 실험' }).hasAttribute('aria-current')).toBe(false)
  expect(new URLSearchParams(location.search).get('view')).toBe('costs')
  await screen.findByRole('table', { name: '비용별 핵심 지표' })
})

it.each([
  { name: 'omitted', checks: undefined }, { name: 'null', checks: null },
  { name: 'array', checks: [] }, { name: 'scalar', checks: 'invalid' },
])('renders partial Oracle verification with $name checks', async ({ checks }) => {
  mockApi([fixture(exp30, '30')], undefined, {
    actual_price_validation: { results: [
      { symbol: '900001', start: at, end: nextDay, calculation_dates_checked: 0, source_features_equal_independent_formulas: true },
      { symbol: '900002', distinct_trading_dates: 0, source_features_equal_independent_formulas: true, fields_checked: [] },
    ] },
    actual_financial_validation: { symbol: '900001', division: 'CFS', checks },
  })
  open('experiment=' + exp30)
  fireEvent.click(screen.getByText('자료 범위와 검산 결과'))

  const table = await screen.findByRole('table', { name: '관측 가격 검산' })
  const partial = within(table).getByRole('rowheader', { name: '900001' }).closest('tr')!
  const zero = within(table).getByRole('rowheader', { name: '900002' }).closest('tr')!
  expect(within(partial).getByText('확인 불가')).toBeTruthy()
  expect(within(partial).getByText('계산 날짜 0개 · 일치 지표 확인 불가')).toBeTruthy()
  expect(within(zero).getByText('0일')).toBeTruthy()
  expect(within(zero).getByText('계산 날짜 확인 불가 · 일치 지표 0개')).toBeTruthy()
  const financial = screen.getByRole('heading', { name: '관측 재무 검산' }).closest('section')!
  expect(within(financial).getByText(/대조 항목 확인 불가/)).toBeTruthy()
  expect(within(financial).getByText('확인 불가', { selector: 'strong' })).toBeTruthy()
  expect(financial.textContent).not.toMatch(/undefined|null|불일치/)
})
