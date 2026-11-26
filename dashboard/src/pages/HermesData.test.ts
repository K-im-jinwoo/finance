import { describe, expect, it } from 'vitest'
import { equityPoints, matchingReports, selectedReport, type HermesReport } from './HermesData'

const reports: HermesReport[] = [
  {
    key: 'H-latest',
    report_id: 'R-20261126T0000Z-DEADBEEF',
    published_at: '2026-11-26T00:00:00Z',
    retrieved_at: '2026-11-26T00:02:00Z',
    author: 'stock-cio',
    source: 'HERMES_STOCK_CIO_FINAL_MESSAGE',
    content: '최종판정',
    content_sha256: 'hash',
    selection_status: 'UNSUPPORTED_OR_AMBIGUOUS',
    selected: null,
    candidates: [{
      rank: 1,
      name: '원익IPS',
      symbol: '240810',
      original_review_tier: null,
      original_decision: '매수 보류',
      reason: null,
      conditions: null,
      invalidation: null,
    }],
  },
  {
    key: 'H-previous',
    report_id: null,
    published_at: '2026-11-25T00:00:00Z',
    retrieved_at: '2026-11-25T00:02:00Z',
    author: 'stock-cio',
    source: 'HERMES_STOCK_CIO_FINAL_MESSAGE',
    content: '공시 검토',
    content_sha256: 'other-hash',
    selection_status: 'UNSUPPORTED_OR_AMBIGUOUS',
    selected: null,
    candidates: [],
  },
]

describe('Hermes equity', () => {
  it('uses initial cash as the starting high, keeps missing marks, and carries the high water mark across gaps', () => {
    expect(equityPoints([
      { at: '2026-11-25T00:00:00Z', total_assets: '900' },
      { at: '2026-11-25T01:00:00Z', total_assets: '1250' },
      { at: '2026-11-25T02:00:00Z', total_assets: null },
      { at: '2026-11-25T03:00:00Z', total_assets: '1000' },
    ], '1000')).toEqual([
      { time: '2026-11-25T00:00:00Z', equity: 900, drawdown: -10 },
      { time: '2026-11-25T01:00:00Z', equity: 1250, drawdown: 0 },
      { time: '2026-11-25T02:00:00Z', equity: null, drawdown: null },
      { time: '2026-11-25T03:00:00Z', equity: 1000, drawdown: -20 },
    ])
  })
})

describe('Hermes report selection', () => {
  it('searches report IDs, candidate names and codes, and the original content', () => {
    expect(matchingReports(reports, 'deadbeef')).toEqual([reports[0]])
    expect(matchingReports(reports, ' 원익ips ')).toEqual([reports[0]])
    expect(matchingReports(reports, '240810')).toEqual([reports[0]])
    expect(matchingReports(reports, '공시 검토')).toEqual([reports[1]])
  })

  it('retains an explicit archive selection as newer reports arrive and preserves missing links', () => {
    expect(selectedReport(reports, 'H-previous')).toBe(reports[1])
    expect(selectedReport(reports, 'H-unknown')).toBeUndefined()
    expect(selectedReport(reports, '')).toBeUndefined()
    expect(selectedReport(reports, null)).toBe(reports[0])
  })
})
