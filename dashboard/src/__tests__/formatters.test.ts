import { describe, expect, it } from 'vitest'
import { koreanUnit, kst, money, spreadText, summaryMoney, turnoverText } from '../utils/formatters'

import { evidenceLabel, labelForCode, reportName } from '../utils/labels'

describe('당일 단타 표시 계약', () => {
  it('반올림한 원화와 한국어 자릿수를 표시한다', () => {
    expect(money(-3500.5, true)).toBe('-3,501원')
    expect(money(-0.49, true)).toBe('0원')
    expect(money('3000.5', true)).toBe('+3,001원')
    expect(koreanUnit(10_003_585.5)).toBe('1,000만 3,586원')
    expect(koreanUnit(-100_000_000, true)).toBe('-1억 원')
    expect(koreanUnit(null)).toBe('확인 불가')
    expect(money(true)).toBe('확인 불가')
  })

  it('거래대금과 스프레드 표시를 보존한다', () => {
    expect(turnoverText(558_099_999_999)).toBe('5,580억 원')
    expect(turnoverText(12_999)).toBe('1만 원')
    expect(spreadText(7.7, true)).toBe('0.08% (7.7bps)')
    expect(spreadText(null, false)).toBe('호가 수신 대기')
    expect(spreadText(-1, false)).toBe('확인 불가')
  })
})

it('formats summary money and minute timestamps while keeping fill seconds available', () => {
  expect(summaryMoney(4_234_680_895)).toBe('42.3억 원')
  expect(money(4_234_680_895)).toBe('4,234,680,895원')
  expect(kst('2026-11-27T01:54:57Z')).toBe('26.11.27 10:54')
  expect(kst('2026-11-27T01:54:57Z', true)).toBe('26.11.27 10:54:57')
})

it('distinguishes reports from the same Korean calendar day without exposing their IDs', () => {
  const first = { report_id: 'UR-first', as_of: '2026-11-26T23:47:00Z' }
  const second = { report_id: 'UR-second', as_of: '2026-11-27T09:15:00+09:00' }
  expect(reportName(first, 'research')).toBe('261127 장기 리서치 보고서')
  expect(reportName(first, 'research', [first, second])).toBe('261127 장기 리서치 보고서 (08:47)')
  expect(reportName(second, 'research', [first, second])).toBe('261127 장기 리서치 보고서 (09:15)')
  expect(evidenceLabel('UNKNOWN')).toBeUndefined()
  expect(labelForCode('NEW_BACKEND_CODE')).toBe('확인 불가')
})
