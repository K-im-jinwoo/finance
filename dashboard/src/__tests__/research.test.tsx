import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { PollProvider } from '../api/client'
import Research from '../pages/Research'

const latest = 'UR-aaaaaaaaaaaaaaaaaaaaaaaa'
const previous = 'UR-bbbbbbbbbbbbbbbbbbbbbbbb'
const asOf = '2026-11-25T20:00:00+09:00'
const row = {
  symbol: '005930', name: '삼성전자', action_decision: 'WATCH', label: '관찰',
  financial_score: '84', news_score: '71', risk_penalty: '3', total_score: '77.1',
  financial_breakdown: { profit: { points: '12', maximum: 25, status: 'AVAILABLE', reason: '연결 재무 근거' } },
  news_events: [{ event_id: 'event-1', points: 5, status: 'VERIFIED_ACTIVE' }],
  price: { status: 'READY', strategy: 'MOMENTUM_CONTINUATION', session: '2026-11-25', reasons: [],
    entry_conditions: ['다음 거래일 최신 시세로 유동성과 가격 조건을 다시 확인', '가격이 20일 이동평균 아래로 이탈하면 추천 무효'] },
  recent_news_complete: true, risk_history_complete: false, reasons: [], warnings: ['RISK_COVERAGE_PARTIAL'],
  pending_checks: ['RISK_HISTORY_COVERAGE_UNAVAILABLE'], advisories: ['재무 자문 항목'],
}
const excluded = { ...row, symbol: '000660', name: 'SK하이닉스', action_decision: 'EXCLUDED', label: '제외', total_score: null }
const bar = {
  date: '2026-11-25', open: 81100, high: 82400, low: 80200, close: 81900, volume: 900000,
  sma5: 81000, sma20: 79800, sma60: 79500, sma120: null, atr14: 1800, rsi14: 61.2, volume_ratio: 1.21,
}
const history = Array.from({ length: 59 }, (_, index) => ({
  ...bar, date: new Date(Date.UTC(2026, 8, index + 1)).toISOString().slice(0, 10),
  sma5: index < 4 ? null : bar.sma5, sma20: index < 19 ? null : bar.sma20, sma60: null,
  atr14: index < 14 ? null : bar.atr14, rsi14: index < 14 ? null : 51.3, volume_ratio: index < 20 ? null : bar.volume_ratio,
}))

function open(options: { missing?: boolean; chartMissing?: boolean } = {}) {
  const latestSummary = { report_id: latest, as_of: asOf }
  const previousSummary = { report_id: previous, as_of: '2026-11-24T20:00:00+09:00' }
  const fetchMock = vi.fn(async (path: string) => {
    if (path === '/api/research/reports') return Response.json(options.missing
      ? { present: false, reason: 'DATABASE_MISSING', reports: [], latest: null }
      : { present: true, reports: [latestSummary, previousSummary], latest: latestSummary })
    if (path === '/api/research/reports/' + latest || path === '/api/research/reports/' + previous) {
      const isLatest = path.endsWith(latest)
      return Response.json({ present: true, report: {
        ...(isLatest ? latestSummary : previousSummary), expected_price_session: '2026-11-25',
        score_formula: 'clip(0.7*F + 0.3*N - R, 0, 100)', evaluations: isLatest ? [excluded, row] : [excluded],
        candidates: isLatest ? [{ ...row, rank: 1 }] : [],
      } })
    }
    if (path === '/api/research/chart/005930?report_id=' + latest) return Response.json(options.chartMissing
      ? { present: false, reason: 'BARS_NOT_FOUND', bars: [] }
      : { present: true, report_id: latest, symbol: row.symbol, as_of: asOf, price_session: bar.date,
        bars: [...history, { ...bar, date: '2026-11-24', open: 81500, close: 81000, rsi14: 58.4 }, bar],
        support: [80000], resistance: [83000], analysis_available: true,
        analysis: { trade_plan: { grade_label: '돌파 대기', breakout_price: 83000, support_price: 80000, stop_loss: 79000 } },
        technical: { methods: [{ code: 'OHLC_CANDLE', name: '일봉 캔들', reading: '양봉', limit: '한 봉으로 추세를 확정하지 않음' }] } })
    return Response.json({ error: 'NOT_FOUND' }, { status: 404 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return render(<PollProvider defaultInterval={0}><Research /></PollProvider>)
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

it('loads latest and past reports, including excluded evaluations, and filters by symbol and verdict', async () => {
  open()
  const table = await screen.findByRole('table', { name: '종목별 리서치 판정' })
  const watch = within(table).getByRole('button', { name: '삼성전자 005930' })
  expect(watch).toBeTruthy()
  fireEvent.click(within(table).getByRole('button', { name: '총점' }))
  expect(within(table).getByRole('columnheader', { name: '총점' }).getAttribute('aria-sort')).toBe('descending')
  expect(within(table).getAllByRole('row')[1].textContent).toContain('삼성전자')

  const verdict = screen.getByRole('combobox', { name: '판정' })
  fireEvent.change(verdict, { target: { value: 'EXCLUDED' } })
  expect(within(table).queryByRole('button', { name: '삼성전자 005930' })).toBeNull()
  expect(within(table).getByRole('button', { name: 'SK하이닉스 000660' })).toBeTruthy()
  const search = screen.getByRole('searchbox', { name: '종목 검색' })
  fireEvent.change(search, { target: { value: '005930' } })
  expect(screen.getByText('검색·필터 조건에 맞는 종목이 없습니다.')).toBeTruthy()

  fireEvent.change(search, { target: { value: '' } })
  fireEvent.change(verdict, { target: { value: '' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'UR 보고서' }), { target: { value: previous } })
  expect(await screen.findByRole('button', { name: 'SK하이닉스 000660' })).toBeTruthy()
  expect(screen.queryByRole('button', { name: '삼성전자 005930' })).toBeNull()
})

it('opens a report-bound drawer with recorded evidence and a keyboard-selectable candlestick chart', async () => {
  const { container } = open()
  const trigger = await screen.findByRole('button', { name: '삼성전자 005930' })
  trigger.focus()
  fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog', { name: '삼성전자 005930' })
  const details = within(dialog)
  expect(details.getByText('연결 재무 근거')).toBeTruthy()
  expect(details.getByText('event-1')).toBeTruthy()
  expect(details.getByText('RISK_HISTORY_COVERAGE_UNAVAILABLE')).toBeTruthy()
  expect(details.getByText('재무 자문 항목')).toBeTruthy()
  expect(details.getByText('가격이 20일 이동평균 아래로 이탈하면 추천 무효')).toBeTruthy()

  const day = await details.findByRole('slider', { name: '차트 거래일' })
  expect(day.getAttribute('aria-valuetext')).toBe('2026-11-25')
  expect(details.getByText('돌파 대기')).toBeTruthy()
  expect(details.getByText('지지 80,000원')).toBeTruthy()
  fireEvent.change(day, { target: { value: '59' } })
  expect(day.getAttribute('aria-valuetext')).toBe('2026-11-24')
  expect(details.getByText('-500원')).toBeTruthy()
  const average = details.getByRole('button', { name: 'SMA 20' })
  expect(average.getAttribute('aria-pressed')).toBe('true')
  expect(container.querySelector('.research-chart-sma-20')).toBeTruthy()
  fireEvent.click(average)
  expect(average.getAttribute('aria-pressed')).toBe('false')
  expect(container.querySelector('.research-chart-sma-20')).toBeNull()
  fireEvent.click(details.getByText('거래일별 데이터 표'))
  const data = await details.findByRole('table', { name: '거래일별 OHLCV와 기술 지표' })
  expect(within(data).getByText('61.2')).toBeTruthy()
  expect(container.querySelector('[style]')).toBeNull()
  fireEvent.click(details.getByRole('button', { name: '상세 닫기' }))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(document.activeElement).toBe(trigger)
})

it('opens the exact report and candlestick from a symbol deep link', async () => {
  window.history.replaceState(null, '', '/research?report=' + latest + '&symbol=005930')
  open()
  const dialog = await screen.findByRole('dialog', { name: '삼성전자 005930' })
  expect(await within(dialog).findByRole('slider', { name: '차트 거래일' })).toBeTruthy()
  fireEvent.click(within(dialog).getByRole('button', { name: '상세 닫기' }))
  expect(location.search).toBe('?report=' + latest)
})

it('distinguishes missing report storage and a missing chart for a present report', async () => {
  const { unmount } = open({ missing: true })
  expect(await screen.findByText('연결할 로컬 데이터베이스 파일이 없습니다.')).toBeTruthy()
  expect(screen.queryByRole('table', { name: '종목별 리서치 판정' })).toBeNull()
  unmount()

  open({ chartMissing: true })
  fireEvent.click(await screen.findByRole('button', { name: '삼성전자 005930' }))
  expect(await screen.findByText('보고서 기준으로 표시할 저장 일봉이 없습니다.')).toBeTruthy()
  expect(screen.queryByRole('slider', { name: '차트 거래일' })).toBeNull()
})
