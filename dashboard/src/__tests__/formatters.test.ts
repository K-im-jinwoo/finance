import { describe, expect, it } from 'vitest'
import { koreanUnit, money, spreadText, turnoverText } from '../utils/formatters'

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
