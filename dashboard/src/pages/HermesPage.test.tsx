import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CapabilitiesProvider, PollProvider, useRefresh } from '../api/client'
import Hermes from './Hermes'
import type { Candidate, HermesAccount, HermesReport } from './HermesData'

const first = 'P-aaaaaaaaaaaaaaaaaaaaaaaa'
const second = 'P-bbbbbbbbbbbbbbbbbbbbbbbb'
const newer = 'P-cccccccccccccccccccccccc'
const accountPath = '/api/paper-accounts'
const received = '2026-11-26T00:03:00Z'
const reportHash = '7'.repeat(64)

const candidate: Candidate = {
  rank: 1,
  name: '테스트 종목',
  symbol: '111111',
  original_decision: '매수 보류',
  original_review_tier: '우선 검토 후보',
  reason: 'CIO 검토 근거',
  conditions: '관측 자료 검토',
  invalidation: '무효화 검토 조건',
}
const latest: HermesReport = {
  source: 'HERMES_STOCK_CIO_FINAL_MESSAGE',
  author: 'stock-cio',
  key: 'H-recent',
  report_id: 'R-RECENT',
  published_at: '2026-11-26T00:00:00Z',
  retrieved_at: received,
  content_sha256: '8'.repeat(64),
  content: '최신 CIO 원문',
  selection_status: 'EXPLICIT_RANK_1',
  selected: candidate,
  candidates: [candidate],
}
const archived: HermesReport = {
  ...latest,
  key: 'H-archive',
  report_id: 'R-ARCHIVE',
  published_at: '2026-11-25T00:00:00Z',
  content_sha256: reportHash,
  content: '이전 CIO 원문 <script>원문을 텍스트로만 표시</script>',
}

function account(id: string): HermesAccount {
  const priceCheck = { code: 'PRICE_LT_SMA20', status: 'INVALID', close: '9000', sma20: '10000' }
  return {
    account_id: id,
    as_of: received,
    created_at: received,
    current_assets: '10000120',
    current_return: '0.000012',
    coverage: { mode: 'OBSERVED', limitations: [] },
    metrics: { max_drawdown: '0', valuation_missing_count: 1 },
    policy: {
      initial_cash: '10000000', entry_fraction: '0.2', max_positions: 5,
      round_trip_bps: '30', policy_status: 'LOCAL_VALIDATION_PROPOSAL', proposals: [],
    },
    meta: {
      selection: 'HERMES_CIO_EXPLICIT_RANK_1',
      feed: {
        mode: 'DEDICATED_PUBLIC_REST', auth_mode: 'SHARED_CLIENT_TOKEN_CACHE',
        execution_gate: 'OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED',
      },
      calendar: null,
    },
    state: {
      status: 'RUNNING',
      cash: '9990120',
      realized_pnl: '0',
      equity: [
        { at: '2026-11-25T09:00:00Z', total_assets: '10000120' },
        { at: '2026-11-25T09:01:00Z', total_assets: null },
        { at: '2026-11-25T09:02:00Z', total_assets: '10000300' },
      ],
      positions: { '111111': { quantity: 1, entry_price: '9870' } },
      pending: {
        '111111': { side: 'SELL', created_at: received, report_id: 'R-PAPER', causes: [priceCheck] },
        '222222': { side: 'BUY', created_at: received, report_id: 'R-BUY' },
      },
      checks: {
        '111111': [priceCheck, { code: 'GENERAL_FINANCIAL', status: 'UNKNOWN' }, { code: 'DISCLOSURE', status: 'REVIEW_REQUIRED' }],
      },
      trades: [{
        trade_id: 'paper-buy', side: 'BUY', symbol: '111111', quantity: 1, price: '9870',
        fee: '15', filled_at: '2026-11-25T09:01:00Z', report_id: 'R-PAPER',
      }],
    },
    securities: { '111111': { name: candidate.name }, '222222': { name: '편입 후보' } },
    reports: [],
    position_quotes: {
      '111111': {
        price: '10000', pnl: null, return: null, freshness: 'STALE',
        age_seconds: 1000, source: 'PUBLIC_TEST_FIXTURE', source_timestamp: received,
      },
    },
    watch_quotes: {
      '111111': {
        price: '10200', pnl: null, return: null, freshness: 'FRESH',
        age_seconds: 42, source: 'PUBLIC_TEST_FIXTURE', source_timestamp: received,
      },
    },
  }
}

function RefreshButton() {
  const refresh = useRefresh()
  return <button type="button" onClick={refresh}>테스트 새로고침</button>
}

function renderPage() {
  return render(
    <PollProvider defaultInterval={0}>
      <CapabilitiesProvider value={{
        read_only: false, orders_enabled: false, telegram_delivery_enabled: false,
        phase_b_available: false, routes: ['/hermes'],
      }}>
        <Hermes />
        <RefreshButton />
      </CapabilitiesProvider>
    </PollProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('keeps account and report choices when polling replaces the latest entries', async () => {
  window.history.replaceState(null, '', '/hermes?keep=1#source')
  let accountIds = [first, second]
  let reports = [latest, archived]
  const fetchMock = vi.fn(async (path: string) => {
    if (path === accountPath) {
      return Response.json({ accounts: accountIds, source: { state: 'CONNECTED' } })
    }
    const id = path.slice(accountPath.length + 1)
    return Response.json({ ...account(id), reports })
  })
  vi.stubGlobal('fetch', fetchMock)
  const { container } = renderPage()

  const selector = await screen.findByLabelText<HTMLSelectElement>('모의계좌 선택')
  await screen.findByRole('heading', { name: '계좌 자산 및 낙폭' })
  expect(selector.value).toBe(first)
  expect(new URLSearchParams(window.location.search).get('account')).toBe(first)
  fireEvent.change(selector, { target: { value: second } })
  await screen.findByRole('heading', { name: '계좌 자산 및 낙폭' })
  expect(new URLSearchParams(window.location.search).get('account')).toBe(second)

  fireEvent.change(screen.getByLabelText('보고서 검색'), { target: { value: 'archive' } })
  expect(screen.getByText('검색 결과 1건')).toBeDefined()
  fireEvent.change(screen.getByLabelText('보고서 선택'), { target: { value: archived.key } })
  expect(new URLSearchParams(window.location.search).get('report')).toBe(archived.key)
  fireEvent.click(screen.getByText('Hermes 최종판정 원문 전체'))
  expect(screen.getByText(reportHash)).toBeDefined()
  expect(container.querySelector('.report-text')?.textContent).toBe(archived.content)
  expect(container.querySelector('.report-text script')).toBeNull()
  expect(screen.getByText('경과 42초')).toBeDefined()
  expect(screen.getByText('STALE · 오래된 시세')).toBeDefined()
  expect(screen.getByText('매도 대기')).toBeDefined()
  expect(screen.getByText('편입 대기')).toBeDefined()
  expect(screen.getByText('INVALID')).toBeDefined()
  expect(screen.getByText('REVIEW_REQUIRED')).toBeDefined()
  expect(container.querySelectorAll('.chart-equity[data-segment]')).toHaveLength(2)
  expect(container.querySelector('.chart-fill-buy')).not.toBeNull()

  accountIds = [newer, first, second]
  reports = [{ ...latest, key: 'H-coming', report_id: 'R-COMING' }, ...reports]
  fireEvent.click(screen.getByRole('button', { name: '테스트 새로고침' }))
  await waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => path.endsWith(second))).toHaveLength(2))
  await waitFor(() => expect(screen.getByRole('option', { name: newer })).toBeDefined())
  expect(selector.value).toBe(second)
  expect(screen.getByLabelText<HTMLSelectElement>('보고서 선택').value).toBe(archived.key)
  expect(container.querySelector('.report-text')?.closest('details')?.open).toBe(true)
  expect(window.location.search).toContain('keep=1')
  expect(window.location.hash).toBe('#source')
})

it('preserves unknown account and report links until the user changes the selection', async () => {
  window.history.replaceState(null, '', '/hermes?account=missing&report=H-missing')
  const fetchMock = vi.fn(async (path: string) => path === accountPath
    ? Response.json({ accounts: [first], source: { state: 'CONNECTED' } })
    : Response.json([latest, archived]))
  vi.stubGlobal('fetch', fetchMock)
  renderPage()

  await screen.findByText('요청한 계좌를 찾을 수 없습니다. 계좌 목록에서 다른 계좌를 선택할 수 있습니다.')
  await screen.findByText('선택한 원본 보고서를 찾을 수 없습니다.')
  expect(window.location.search).toBe('?account=missing&report=H-missing')
  expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([accountPath, '/api/hermes/reports'])
})
