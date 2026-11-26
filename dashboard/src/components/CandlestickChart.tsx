import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { money, numeric } from '../utils/formatters'
import { Empty, Pnl, Table } from './ui'
import './candlestick-chart.css'

export type CandleBar = {
  date: string; open: number; high: number; low: number; close: number; volume: number
  sma5?: number | null; sma20?: number | null; sma60?: number | null; sma120?: number | null
  atr14?: number | null; rsi14?: number | null; volume_ratio?: number | null
}

const averages = [5, 20, 60, 120] as const
type Average = typeof averages[number]
type Indicator = `sma${Average}` | 'atr14' | 'rsi14'
type Scale = (value: number) => number

const start = 63
const endPadding = 12
const chartHeight = 421
const compact = new Intl.NumberFormat('ko-KR', { notation: 'compact', maximumFractionDigits: 1 })
const axisNumber = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 1 })

function scale(min: number, max: number, top: number, bottom: number): Scale {
  return value => bottom - (value - min) / (max - min || 1) * (bottom - top)
}

function Axis({ label, ticks, y, right, titleY, format = axisNumber.format }: {
  label: string; ticks: number[]; y: Scale; right: number; titleY: number; format?: (value: number) => string
}) {
  return <g>
    <text className="research-chart-axis-title" x={start} y={titleY}>{label}</text>
    {ticks.map(value => <g key={value}>
      <line className="chart-gridline" x1={start} y1={y(value)} x2={right} y2={y(value)} />
      <text className="research-chart-axis-text" x={start - 7} y={y(value) + 4} textAnchor="end">{format(value)}</text>
    </g>)}
  </g>
}

function IndicatorLine({ bars, field, x, y, className }: {
  bars: CandleBar[]; field: Indicator; x: Scale; y: Scale; className: string
}) {
  const segments: { x: number; y: number }[][] = []
  let segment: { x: number; y: number }[] | undefined
  bars.forEach((bar, index) => {
    const value = bar[field]
    if (value == null || !Number.isFinite(value)) {
      segment = undefined
      return
    }
    if (!segment) {
      segment = []
      segments.push(segment)
    }
    segment.push({ x: x(index), y: y(value) })
  })
  return <>{segments.map((points, index) => points.length === 1
    ? <circle key={index} className={className} cx={points[0].x} cy={points[0].y} r="1.5" />
    : <path key={index} className={className} d={points.map((point, i) => `${i ? 'L' : 'M'}${point.x},${point.y}`).join(' ')} />
  )}</>
}

function candleDirection(bar: CandleBar) {
  return bar.close > bar.open ? 'up' : bar.close < bar.open ? 'down' : 'flat'
}

function CandleData({ bars }: { bars: CandleBar[] }) {
  const [open, setOpen] = useState(false)
  return <details className="research-chart-data" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>거래일별 데이터 표</summary>
    {open && <Table label="거래일별 OHLCV와 기술 지표" columns={[
      '거래일', '시가', '고가', '저가', '종가', '시가 대비', '거래량', '거래량 배율',
      ...averages.map(period => `SMA ${period}`), 'Wilder ATR14', 'RSI14',
    ]}>
      {[...bars].reverse().map(bar => <tr key={bar.date} className="mono">
        <th scope="row">{bar.date}</th>
        <td>{money(bar.open)}</td><td>{money(bar.high)}</td><td>{money(bar.low)}</td><td>{money(bar.close)}</td>
        <td><Pnl value={bar.close - bar.open} format={money} /></td>
        <td>{numeric(bar.volume, '주')}</td><td>{numeric(bar.volume_ratio, '배')}</td>
        {averages.map(period => <td key={period}>{numeric(bar[`sma${period}`], '원')}</td>)}
        <td>{numeric(bar.atr14, '원')}</td><td>{numeric(bar.rsi14)}</td>
      </tr>)}
    </Table>}
  </details>
}

export function CandlestickChart({ bars, support = [], resistance = [] }: {
  bars: CandleBar[]; support?: number[]; resistance?: number[]
}) {
  const container = useRef<HTMLElement>(null)
  const caption = useId()
  const title = useId()
  const [width, setWidth] = useState(410)
  const [visible, setVisible] = useState<Average[]>([...averages])
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const ordered = useMemo(() => [...bars].sort((left, right) => left.date.localeCompare(right.date)), [bars])

  useEffect(() => {
    const element = container.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.round(entry.contentRect.width))))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  if (!ordered.length) return <Empty>표시할 저장 일봉이 없습니다.</Empty>
  const selectedIndex = selectedDate === null ? -1 : ordered.findIndex(bar => bar.date === selectedDate)
  const active = selectedIndex < 0 ? ordered.length - 1 : selectedIndex
  const bar = ordered[active]
  const right = width - endPadding
  const step = (right - start) / ordered.length
  const bodyWidth = Math.min(12, Math.max(1, step * 0.64))
  const x = (index: number) => start + (index + 0.5) * step

  const prices = ordered.flatMap(item => [item.low, item.high,
    ...averages.flatMap(period => {
      const value = item[`sma${period}`]
      return visible.includes(period) && value != null && Number.isFinite(value) ? [value] : []
    }),
  ])
  const priceLevels = [...support, ...resistance].filter(Number.isFinite)
  const low = Math.min(...prices, ...priceLevels)
  const high = Math.max(...prices, ...priceLevels)
  const pad = (high - low || Math.abs(high) || 1) * 0.04
  const yPrice = scale(low - pad, high + pad, 27, 186)
  const maxVolume = Math.max(1, ...ordered.map(item => item.volume))
  const yVolume = scale(0, maxVolume, 221, 266)
  const maxAtr = Math.max(1, ...ordered.map(item => item.atr14 ?? 0))
  const yAtr = scale(0, maxAtr, 301, 333)
  const yRsi = scale(0, 100, 368, 401)
  const ticks = [...new Set([0, ...(width >= 330 && ordered.length > 2 ? [Math.floor((ordered.length - 1) / 2)] : []), ordered.length - 1])]
  const dir = candleDirection(bar)

  function pick(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width <= 0) return
    const plotX = (event.clientX - box.left) * width / box.width
    const nearest = Math.floor((plotX - start) / step)
    if (nearest >= 0 && nearest < ordered.length) setSelectedDate(ordered[nearest].date)
  }

  return <figure ref={container} className="stack research-chart" aria-labelledby={caption}>
    <figcaption id={caption} className="muted">일봉 · {ordered[0].date} ~ {ordered.at(-1)!.date}</figcaption>
    <div className="cluster research-chart-controls" role="group" aria-label="이동평균선 표시">
      {averages.map(period => <button key={period} type="button" aria-pressed={visible.includes(period)}
        onClick={() => setVisible(previous => previous.includes(period) ? previous.filter(item => item !== period) : [...previous, period])}>
        <span className={`research-chart-key research-chart-key-${period}`} aria-hidden="true" />SMA {period}
      </button>)}
    </div>
    <svg className="research-chart-svg" role="img" aria-labelledby={title} viewBox={`0 0 ${width} ${chartHeight}`}
      onPointerMove={event => { if (event.pointerType !== 'touch') pick(event) }} onPointerDown={pick}>
      <title id={title}>일봉 캔들, 거래량, Wilder ATR14, RSI14</title>
      <Axis label="가격 (원)" ticks={[low, ...(high !== low ? [(low + high) / 2, high] : [])]} y={yPrice} right={right} titleY={16} />
      <Axis label="거래량 (주)" ticks={[0, maxVolume]} y={yVolume} right={right} titleY={210} format={compact.format} />
      <Axis label="Wilder ATR14 (원)" ticks={[0, maxAtr]} y={yAtr} right={right} titleY={290} />
      <Axis label="RSI14" ticks={[30, 70]} y={yRsi} right={right} titleY={357} />
      {support.filter(Number.isFinite).map((value, index) => <line key={index} className="research-chart-support"
        x1={start} x2={right} y1={yPrice(value)} y2={yPrice(value)} />)}
      {resistance.filter(Number.isFinite).map((value, index) => <line key={index} className="research-chart-resistance"
        x1={start} x2={right} y1={yPrice(value)} y2={yPrice(value)} />)}
      <line className="research-chart-cursor" x1={x(active)} x2={x(active)} y1="27" y2="401" />
      {ordered.map((item, index) => {
        const candleX = x(index)
        const open = yPrice(item.open), close = yPrice(item.close)
        return <g key={item.date} className={`research-candle research-candle-${candleDirection(item)}`}>
          <line className="research-candle-wick" x1={candleX} x2={candleX} y1={yPrice(item.high)} y2={yPrice(item.low)} />
          <rect className="research-candle-body" x={candleX - bodyWidth / 2} y={Math.min(open, close)} width={bodyWidth}
            height={Math.max(1, Math.abs(open - close))} />
          <rect className="research-candle-volume" x={candleX - bodyWidth / 2} y={yVolume(item.volume)} width={bodyWidth}
            height={266 - yVolume(item.volume)} />
        </g>
      })}
      {averages.filter(period => visible.includes(period)).map(period => <IndicatorLine key={period} bars={ordered} field={`sma${period}`}
        x={x} y={yPrice} className={`research-chart-series research-chart-sma-${period}`} />)}
      <IndicatorLine bars={ordered} field="atr14" x={x} y={yAtr} className="research-chart-series research-chart-atr" />
      <IndicatorLine bars={ordered} field="rsi14" x={x} y={yRsi} className="research-chart-series research-chart-rsi" />
      {ticks.map((index, tick) => <text key={index} className="research-chart-axis-text"
        x={ticks.length === 1 ? x(index) : tick === 0 ? start : tick === ticks.length - 1 ? right : x(index)} y="417"
        textAnchor={ticks.length === 1 ? 'middle' : tick === 0 ? 'start' : tick === ticks.length - 1 ? 'end' : 'middle'}>
        {ordered[index].date.slice(5)}
      </text>)}
    </svg>
    {(support.length > 0 || resistance.length > 0) && <div className="cluster research-chart-levels">
      {support.length > 0 && <span><i className="research-chart-key research-chart-key-support" aria-hidden="true" />지지 {support.map(value => money(value)).join(', ')}</span>}
      {resistance.length > 0 && <span><i className="research-chart-key research-chart-key-resistance" aria-hidden="true" />저항 {resistance.map(value => money(value)).join(', ')}</span>}
    </div>}
    <label className="research-chart-date">
      <span>차트 거래일</span><span className="mono" aria-hidden="true">{bar.date}</span>
      <input type="range" min="0" max={ordered.length - 1} step="1" value={active} disabled={ordered.length === 1}
        aria-valuetext={bar.date} onChange={event => setSelectedDate(ordered[Number(event.target.value)].date)} />
    </label>
    <div className="cluster research-chart-direction"><span>{dir === 'up' ? '양봉' : dir === 'down' ? '음봉' : '시가·종가 동일'}</span>
      <Pnl value={bar.close - bar.open} format={money} /><span className="muted">시가 대비</span>
    </div>
    <dl className="research-chart-readout">
      {[
        ['시가', money(bar.open)], ['고가', money(bar.high)], ['저가', money(bar.low)], ['종가', money(bar.close)],
        ['거래량', numeric(bar.volume, '주')], ['직전 20일 대비', numeric(bar.volume_ratio, '배')],
        ['Wilder ATR14', numeric(bar.atr14, '원')], ['RSI14', numeric(bar.rsi14)],
        ...averages.filter(period => visible.includes(period)).map(period => [`SMA ${period}`, numeric(bar[`sma${period}`], '원')]),
      ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="mono">{value}</dd></div>)}
    </dl>
    <CandleData bars={ordered} />
  </figure>
}
