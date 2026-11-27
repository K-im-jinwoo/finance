const integerFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 0 })
const numberFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 4 })
const dateFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Seoul', year: '2-digit', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const dayMonthFormat = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', month: 'long', day: 'numeric',
})

export function finite(value: unknown): number | null {
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && value.trim() === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function wonValue(value: unknown): number | null {
  // Match the original input coercion and round the magnitude before restoring its sign.
  if (value == null || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value))) return null
  const number = Number(value)
  const amount = Math.round(Math.abs(number))
  return amount === 0 ? 0 : number < 0 ? -amount : amount
}

function signFor(value: number, signed: boolean) {
  return value < 0 ? '-' : signed && value > 0 ? '+' : ''
}

export function money(value: unknown, signed = false): string {
  const amount = wonValue(value)
  return amount === null ? '확인 불가' : signFor(amount, signed) + integerFormat.format(Math.abs(amount)) + '원'
}

export function koreanUnit(value: unknown, signed = false): string {
  const amount = wonValue(value)
  if (amount === null) return '확인 불가'
  let remaining = Math.abs(amount)
  if (remaining < 10_000) return money(amount, signed)
  const parts: string[] = []
  for (const [size, suffix] of [[1e12, '조'], [1e8, '억'], [1e4, '만']] as const) {
    const group = Math.floor(remaining / size)
    if (group) parts.push(integerFormat.format(group) + suffix)
    remaining %= size
  }
  if (remaining) parts.push(integerFormat.format(remaining))
  return signFor(amount, signed) + parts.join(' ') + (remaining ? '원' : ' 원')
}

export function turnoverText(value: unknown): string {
  const amount = wonValue(value)
  if (amount === null) return '확인 불가'
  const absolute = Math.abs(amount)
  const size = absolute >= 1e8 ? 1e8 : absolute >= 1e4 ? 1e4 : 1
  return signFor(amount, false) + integerFormat.format(Math.floor(absolute / size)) +
    (size === 1e8 ? '억 원' : size === 1e4 ? '만 원' : '원')
}

export function spreadText(value: unknown, _entryBlocked?: boolean): string {
  if (value == null || value === '') return '호가 수신 대기'
  const bps = Number(value)
  return Number.isFinite(bps) && bps >= 0
    ? (bps / 100).toFixed(2) + '% (' + bps.toFixed(1) + 'bps)' : '확인 불가'
}

export function numeric(value: unknown, unit = ''): string {
  const number = finite(value)
  return number === null ? '확인 불가' : numberFormat.format(number) + unit
}

export function percent(value: unknown): string {
  return numeric(value, '%')
}

function kstParts(value?: string | null) {
  const date = value ? new Date(value) : null
  return date && Number.isFinite(date.getTime())
    ? Object.fromEntries(dateFormat.formatToParts(date).map(part => [part.type, part.value]))
    : null
}

export function kst(value?: string | null, withSeconds = false): string {
  const parts = kstParts(value)
  if (!parts) return '시각 확인 불가'
  const { year, month, day, hour, minute, second } = parts
  return year + '.' + month + '.' + day + ' ' + hour + ':' + minute + (withSeconds ? ':' + second : '')
}

export function compactDate(value?: string | null) {
  const parts = kstParts(value)
  return parts ? parts.year + parts.month + parts.day : null
}

export function monthDay(value?: string | null) {
  const date = value ? new Date(value) : null
  return date && Number.isFinite(date.getTime()) ? dayMonthFormat.format(date) : null
}

export function summaryMoney(value: unknown, signed = false) {
  const amount = wonValue(value)
  if (amount === null) return '확인 불가'
  const absolute = Math.abs(amount)
  for (const [base, unit] of [[1e12, '조'], [1e8, '억'], [1e4, '만']] as const) {
    if (absolute >= base) {
      return signFor(amount, signed) + numberFormat.format(Math.round(absolute / base * 10) / 10) + unit + ' 원'
    }
  }
  return money(value, signed)
}
