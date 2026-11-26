import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { CostComparisonChart, EquityDrawdownChart } from '../components/Charts'

afterEach(cleanup)

const points = [
  { time: '2026-11-25T09:00:00+09:00', equity: 100, drawdown: 0 },
  { time: '2026-11-25T09:01:00+09:00', equity: null, drawdown: null },
  { time: '2026-11-25T09:02:00+09:00', equity: 98, drawdown: -2 },
]

it('splits missing marks and exposes fills and keyboard data without inline styles', () => {
  const { container } = render(<EquityDrawdownChart label="테스트 자산" points={points}
    fills={[{ time: points[1].time, label: '005930 가상 매수', side: 'BUY' }]} />)
  expect(container.querySelectorAll('.chart-equity[data-segment]')).toHaveLength(2)
  expect(container.querySelector('.chart-fill-buy')).not.toBeNull()
  const hit = container.querySelector('.chart-marker > circle.chart-hit')
  expect(hit?.getAttribute('r')).toBe('14')
  expect(hit?.getAttribute('fill')).toBe('transparent')
  expect(hit?.getAttribute('pointer-events')).toBe('all')
  fireEvent.mouseEnter(hit!)
  expect(container.querySelector('.chart-focus')?.textContent).toContain('005930 가상 매수')
  expect(container.querySelector('style, [style]')).toBeNull()
  const details = screen.getByText('차트 데이터 보기').closest('details')!
  expect(screen.queryByRole('table', { name: '테스트 자산 데이터' })).toBeNull()
  details.open = true
  fireEvent(details, new Event('toggle'))
  expect(screen.getByRole('table', { name: '테스트 자산 데이터' }).textContent).toContain('확인 불가')
})

it('uses different stroke patterns for the three cost assumptions', () => {
  const { container } = render(<CostComparisonChart series={[
    { bps: 10, points }, { bps: 30, points }, { bps: 60, points },
  ]} />)
  expect(container.querySelector('.chart-cost-10')?.getAttribute('stroke-dasharray')).toBeNull()
  expect(container.querySelector('.chart-cost-30')?.getAttribute('stroke-dasharray')).toBe('8 5')
  expect(container.querySelector('.chart-cost-60')?.getAttribute('stroke-dasharray')).toBe('2 5')
})
