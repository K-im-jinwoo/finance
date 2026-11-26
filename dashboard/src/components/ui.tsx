import { ArrowDown, ArrowUp, X } from 'lucide-react'
import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import { numeric } from '../utils/formatters'

type Tone = 'ok' | 'warn' | 'danger' | 'unknown'

export function Badge({ children, tone = 'unknown' }: { children: ReactNode; tone?: Tone }) {
  return <span className={'badge badge-' + tone}>{children}</span>
}

export function Panel({ title, children, actions }: { title: ReactNode; children: ReactNode; actions?: ReactNode }) {
  const id = useId()
  return <section className="panel" aria-labelledby={id}>
    <header className="section-header">
      <h2 id={id}>{title}</h2>
      {actions != null && <div className="cluster">{actions}</div>}
    </header>
    <div className="panel-content stack">{children}</div>
  </section>
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>
}

export function ResourceState({ loading, error }: { loading: boolean; error: string | null }) {
  if (!loading && !error) return null
  return <p className={error ? 'notice notice-danger' : 'muted'} role="status">
    {error ?? '기록을 불러오는 중입니다…'}
  </p>
}

export function Pnl({ value, format = numeric, signedText }: {
  value: number | null | undefined
  format?: (value: number) => string
  signedText?: string
}) {
  if (value == null || !Number.isFinite(value)) return <span className="muted">확인 불가</span>
  const positive = value > 0
  const negative = value < 0
  const Icon = positive ? ArrowUp : ArrowDown
  const text = signedText ?? ((positive ? '+' : negative ? '-' : '') + format(Math.abs(value)))
  const tone = positive ? 'up' : negative ? 'down' : 'flat'
  return <span className={'pnl pnl-' + tone}>
    {(positive || negative) && <Icon size={15} aria-hidden="true" />}
    <span>{text}</span>
  </span>
}

type Sort = {
  column: number
  direction: 'ascending' | 'descending'
  sortableColumns: number[]
  onChange: (column: number) => void
}

export function Table({ label, columns, children, sort }: {
  label: string; columns: string[]; children: ReactNode; sort?: Sort
}) {
  return <div className="table-scroll" role="region" aria-label={label + ' 스크롤 영역'} tabIndex={0}>
    <table aria-label={label}>
      <thead>
        <tr>{columns.map((title, index) => {
          const direction = sort?.column === index ? sort.direction : undefined
          return <th key={title} scope="col" aria-sort={direction}>
            {sort?.sortableColumns.includes(index)
              ? <button className="table-sort" onClick={() => sort.onChange(index)}>
                  {title} {direction === 'ascending' && <ArrowUp size={14} aria-hidden="true" />}
                  {direction === 'descending' && <ArrowDown size={14} aria-hidden="true" />}
                </button>
              : title}
          </th>
        })}</tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  </div>
}

export function MetricGrid({ metrics }: { metrics: { label: string; value: ReactNode; detail?: ReactNode }[] }) {
  return <dl className="metric-grid">
    {metrics.map(({ label, value, detail }) => <div key={label} className="metric">
      <dt>{label}</dt>
      <dd className="metric-value">{value}</dd>
      {detail != null && <dd className="metric-detail">{detail}</dd>}
    </div>)}
  </dl>
}

export function Drawer({ title, children, onClose }: { title: ReactNode; children: ReactNode; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const id = useId()

  useEffect(() => {
    const element = dialog.current
    const previousFocus = document.activeElement
    if (!element) return
    if (element.showModal) element.showModal()
    else element.setAttribute('open', '')
    return () => {
      if (element.close) element.close()
      if (previousFocus instanceof HTMLElement && document.contains(previousFocus)) previousFocus.focus()
    }
  }, [])

  return <dialog className="drawer" ref={dialog} aria-labelledby={id} onCancel={event => {
    event.preventDefault()
    onClose()
  }}>
    <header className="section-header">
      <h2 id={id}>{title}</h2>
      <button className="icon-button" aria-label="상세 닫기" onClick={onClose}><X size={20} /></button>
    </header>
    <div className="drawer-content stack">{children}</div>
  </dialog>
}
