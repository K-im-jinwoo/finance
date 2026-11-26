import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { koreanUnit, kst, money, percent } from '../utils/formatters'
import { Empty, Pnl, Table } from './ui'

export type ChartPoint = { time: string; equity: number | null; drawdown?: number | null; risk?: string | null; omitted?: boolean }
export type ChartFill = { time: string; label: string; side: string }
type CostSeries = { bps: 10 | 30 | 60; points: ChartPoint[] }
type Plot = { top: number; bottom: number; min: number; max: number }
type PointCoordinate = { x: number; y: number; value: number }

const DEFAULT_WIDTH = 720
const AXIS_GUTTER = 105
const END_PADDING = 20

function usePlotWidth() {
  const figure = useRef<HTMLElement>(null)
  const [width, setWidth] = useState(DEFAULT_WIDTH)
  useEffect(() => {
    const element = figure.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(240, Math.round(entry.contentRect.width)))
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  return { figure, width }
}
const dashes = { 10: undefined, 30: '8 5', 60: '2 5' } as const
const shortTime = new Intl.DateTimeFormat('ko-KR', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Seoul',
})

function chronological<T extends { time: string }>(rows: T[]) {
  return rows.filter(row => Number.isFinite(Date.parse(row.time)))
    .sort((a, b) => Date.parse(a.time) - Date.parse(b.time))
}

function values<T extends { time: string }>(rows: T[], getValue: (row: T) => number | null | undefined) {
  return rows.flatMap(row => {
    const value = getValue(row)
    return value != null && Number.isFinite(value) ? [value] : []
  })
}

function bounds(items: number[], min = 0, max = 0) {
  if (items.length) {
    min = items.reduce((extreme, item) => Math.min(extreme, item), Infinity)
    max = items.reduce((extreme, item) => Math.max(extreme, item), -Infinity)
  }
  const padding = (max - min || Math.abs(max) || 1) * (min === max ? 0.025 : 0.08)
  return { min: min - padding, max: max + padding }
}

function domain(rows: { time: string }[], width: number) {
  const timestamps = rows.map(row => Date.parse(row.time)).filter(Number.isFinite)
  if (!timestamps.length) return null
  const first = timestamps.reduce((a, b) => Math.min(a, b), Infinity)
  const last = timestamps.reduce((a, b) => Math.max(a, b), -Infinity)
  const end = width - END_PADDING
  return {
    first, last,
    x: (time: string | number) => {
      if (first === last) return (AXIS_GUTTER + end) / 2
      const instant = typeof time === 'string' ? Date.parse(time) : time
      return AXIS_GUTTER + (instant - first) / (last - first) * (end - AXIS_GUTTER)
    },
  }
}

function yValue(value: number, plot: Plot) {
  return plot.bottom - (value - plot.min) / (plot.max - plot.min) * (plot.bottom - plot.top)
}

function SegmentedLine({ points, x, field, plot, className, dash }: {
  points: ChartPoint[]; x: (time: string) => number; field: 'equity' | 'drawdown'
  plot: Plot; className: string; dash?: string
}) {
  const segments: PointCoordinate[][] = []
  let current: PointCoordinate[] | null = null
  for (const point of points) {
    const value = point[field]
    if (value == null || !Number.isFinite(value)) {
      current = null
      continue
    }
    if (!current) {
      current = []
      segments.push(current)
    }
    current.push({ x: x(point.time), y: yValue(value, plot), value })
  }
  return <>{segments.map((segment, index) => {
    const first = segment[0]
    if (segment.length === 1) return <circle key={index} className={className} data-segment=""
      cx={first.x} cy={first.y} r="2.5" strokeDasharray={dash} />
    const path = segment.map(({ x, y }, i) => (i === 0 ? 'M' : 'L') + x + ',' + y).join(' ')
    return <path key={index} className={className} data-segment="" strokeDasharray={dash} d={path} />
  })}</>
}

function Axis({ plot, format, title, width }: { plot: Plot; format: (n: number) => string; title: string; width: number }) {
  return <g aria-hidden="true">
    <text className="chart-axis-title" x={AXIS_GUTTER} y={plot.top - 10}>{title}</text>
    {[0, 0.5, 1].map(ratio => {
      const value = plot.min + (plot.max - plot.min) * ratio
      const y = yValue(value, plot)
      return <g key={ratio}>
        <line className="chart-gridline" x1={AXIS_GUTTER} y1={y} x2={width - END_PADDING} y2={y} />
        <text className="chart-axis-text" x={AXIS_GUTTER - 8} y={y + 4} textAnchor="end">{format(value)}</text>
      </g>
    })}
  </g>
}

function TimeAxis({ first, last, x, y }: NonNullable<ReturnType<typeof domain>> & { y: number }) {
  const moments = first === last ? [first] : x(last) - x(first) < 300 ? [first, last] : [first, first + (last - first) / 2, last]
  return <g aria-hidden="true">
    {moments.map((at, index) => <text key={at} className="chart-axis-text" x={x(at)} y={y}
      textAnchor={moments.length === 1 ? 'middle' : index === 0 ? 'start' : index === moments.length - 1 ? 'end' : 'middle'}>
      {shortTime.format(at)}
    </text>)}
  </g>
}

function RiskBands({ points, x, plot, last }: {
  points: ChartPoint[]; x: (time: string | number) => number; plot: Plot; last: number
}) {
  return <>{points.map((point, index) => {
    if (!point.risk) return null
    const from = x(point.time)
    const to = points[index + 1] ? x(points[index + 1].time) : x(last)
    return <rect key={index} className="chart-risk" x={from} y={plot.top}
      width={Math.max(2, to - from)} height={plot.bottom - plot.top}><title>{point.risk}</title></rect>
  })}</>
}

function ChartData({ label, count, children }: {
  label: string; count: number; children: (start: number, end: number) => ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [requestedPage, setPage] = useState(0)
  const pageSize = 200
  const pages = Math.max(1, Math.ceil(count / pageSize))
  const page = Math.min(requestedPage, pages - 1)
  return <details className="chart-data" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open && <>
      {children(page * pageSize, (page + 1) * pageSize)}
      {pages > 1 && <div className="cluster">
        <button disabled={page === 0} onClick={() => setPage(page - 1)}>이전</button>
        <span className="mono" aria-live="polite">{page + 1} / {pages}페이지</span>
        <button disabled={page === pages - 1} onClick={() => setPage(page + 1)}>다음</button>
      </div>}
    </>}
  </details>
}

export function EquityDrawdownChart({ points, fills = [], label }: {
  points: ChartPoint[]; fills?: ChartFill[]; label: string
}) {
  const ordered = useMemo(() => chronological(points), [points])
  const keyedFills = useMemo(() => chronological(fills), [fills])
  const [focus, setFocus] = useState<string | null>(null)
  const { figure, width } = usePlotWidth()
  const timeDomain = domain([...ordered, ...keyedFills], width)
  if (!timeDomain) return <figure ref={figure}><Empty>표시할 자산 평가 시점이 없습니다.</Empty></figure>
  const marks = values(ordered, point => point.equity)
  const risk = ordered.some(point => point.risk)
  const drawdowns = values(ordered, point => point.drawdown)
  const high = { ...bounds(marks), top: 29, bottom: 157 }
  const minDrawdown = drawdowns.reduce((lowest, value) => Math.min(lowest, value), 0)
  const maxDrawdown = drawdowns.reduce((highest, value) => Math.max(highest, value), 0)
  const low = { min: minDrawdown === 0 ? -1 : minDrawdown * 1.08, max: maxDrawdown, top: 201, bottom: 266 }
  const plotted = new Map(ordered.filter(point => point.equity !== null).map(point => [Date.parse(point.time), point.equity]))
  return <figure ref={figure} className="chart stack">
    <figcaption className="cluster chart-caption">
      <span className="legend"><i className="legend-line legend-equity" />평가자산</span>
      <span className="legend"><i className="legend-line legend-drawdown" />고점 대비 낙폭</span>
      {keyedFills.some(fill => fill.side === 'BUY') && <span className="legend"><i className="legend-buy" />모의 매수</span>}
      {keyedFills.some(fill => fill.side === 'SELL') && <span className="legend"><i className="legend-sell" />모의 매도</span>}
      {risk && <span className="legend"><i className="legend-risk" />위험 제한 구간</span>}
    </figcaption>
    {ordered.some(point => point.equity == null && !point.omitted) &&
      <p className="muted">평가 누락 구간은 선을 연결하지 않습니다.</p>}
    {ordered.some(point => point.omitted) &&
      <p className="muted">원장에서 보관 범위를 넘겨 생략한 구간은 선을 연결하지 않습니다.</p>}
    {marks.length === 0 && <p className="muted">확인 가능한 평가자산이 없습니다.</p>}
    <svg className="plot" viewBox={'0 0 ' + width + ' 296'} role="group" aria-label={label}>
      <RiskBands points={ordered} x={timeDomain.x} plot={high} last={timeDomain.last} />
      <Axis plot={high} format={koreanUnit} title="평가자산 (원)" width={width} />
      <Axis plot={low} format={value => percent(Number(value.toFixed(2)))} title="고점 대비 낙폭 (%)" width={width} />
      <SegmentedLine points={ordered} x={timeDomain.x} field="equity" plot={high} className="chart-equity" />
      <SegmentedLine points={ordered} x={timeDomain.x} field="drawdown" plot={low} className="chart-drawdown" />
      {keyedFills.map((fill, index) => {
        const x = timeDomain.x(fill.time)
        const value = plotted.get(Date.parse(fill.time))
        const y = value == null ? high.bottom + 10 : yValue(value, high)
        const buy = fill.side === 'BUY'
        const marker = 'M' + (x - 5) + ' ' + (y + (buy ? 5 : -5)) + ' L' + x + ' ' +
          (y + (buy ? -5 : 5)) + ' L' + (x + 5) + ' ' + (y + (buy ? 5 : -5)) + ' Z'
        const description = kst(fill.time) + ' · ' + fill.label
        return <g key={index} tabIndex={0} role="img" aria-label={description} className="chart-marker"
          onFocus={() => setFocus(description)} onBlur={() => setFocus(null)}
          onMouseEnter={() => setFocus(description)} onMouseLeave={() => setFocus(null)}>
          <title>{description}</title>
          <circle className="chart-hit" cx={x} cy={y} r={14} fill="transparent" pointerEvents="all" aria-hidden="true" />
          <path className={buy ? 'chart-fill-buy' : 'chart-fill-sell'} d={marker} />
        </g>
      })}
      <TimeAxis {...timeDomain} y={287} />
    </svg>
    {keyedFills.length > 0 && <p className="muted chart-focus" aria-live="polite">
      {focus ?? '체결 표식에 포커스하면 상세 시각과 내용을 표시합니다.'}
    </p>}
    <ChartData label="차트 데이터 보기" count={ordered.length}>{(start, end) =>
      <Table label={label + ' 데이터'} columns={['평가 시각', '평가자산', '고점 대비 낙폭', '위험 상태']}>
        {ordered.slice(start, end).map((point, index) => <tr key={index}>
          <td>{kst(point.time)}</td><td className="mono">{point.omitted ? '보관 범위 생략' : money(point.equity)}</td>
          <td className="mono"><Pnl value={point.equity === null ? null : point.drawdown} format={percent} /></td>
          <td>{point.risk ?? '잠금 표시 없음'}</td>
        </tr>)}
      </Table>}
    </ChartData>
    {keyedFills.length > 0 && <ChartData label="체결 표식 데이터 보기" count={keyedFills.length}>{(start, end) =>
      <Table label={label + ' 체결 표식'} columns={['체결 시각', '내용']}>
        {keyedFills.slice(start, end).map((fill, index) => <tr key={index}><td>{kst(fill.time)}</td><td>{fill.label}</td></tr>)}
      </Table>}
    </ChartData>}
  </figure>
}

export function CostComparisonChart({ series }: { series: CostSeries[] }) {
  const ordered = useMemo(() => series.map(item => ({ ...item, points: chronological(item.points) })), [series])
  const rows = ordered.flatMap(item => item.points.map(point => ({ ...point, bps: item.bps })))
  const { figure, width } = usePlotWidth()
  const timeDomain = domain(rows, width)
  if (!timeDomain) return <figure ref={figure}><Empty>비교할 비용별 자산 평가가 없습니다.</Empty></figure>
  const plot = { ...bounds(values(rows, point => point.equity)), top: 28, bottom: 188 }
  return <figure ref={figure} className="chart stack">
    <figcaption className="cluster chart-caption">{ordered.map(item =>
      <span className="legend" key={item.bps}><i className={'legend-line legend-cost-' + item.bps} />{item.bps}bps</span>)}</figcaption>
    {rows.some(point => point.equity == null) && <p className="muted">평가 누락 구간은 표시하지 않습니다.</p>}
    <svg className="plot" viewBox={'0 0 ' + width + ' 219'} role="img" aria-label="10·30·60bps 비용 비교 자산 추이">
      <Axis plot={plot} format={koreanUnit} title="평가자산 (원)" width={width} />
      {ordered.map(item => <SegmentedLine key={item.bps} points={item.points} x={timeDomain.x} field="equity"
        plot={plot} className={'chart-cost-' + item.bps} dash={dashes[item.bps]} />)}
      <TimeAxis {...timeDomain} y={210} />
    </svg>
    <ChartData label="비용별 차트 데이터 보기" count={rows.length}>{(start, end) =>
      <Table label="비용별 자산 데이터" columns={['왕복 비용', '평가 시각', '평가자산']}>
        {rows.slice(start, end).map((point, index) => <tr key={index}>
          <td>{point.bps}bps</td><td>{kst(point.time)}</td><td className="mono">{money(point.equity)}</td>
        </tr>)}
      </Table>}
    </ChartData>
  </figure>
}
