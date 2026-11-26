import { describe, expect, it } from 'vitest'
import { equityPoints, experimentQuery, resolveExperimentEvidence } from '../pages/experimentModel'

const trade = { trade_id: 't1', report_id: 'r1' }
const event = { event_id: 'e1', detail: { trade_id: 't1', report_id: 'r1' } }
const records = {
  trades: [trade, { trade_id: 't2', report_id: 'r1' }],
  outbox: [event, { event_id: 'e2', detail: { trade_id: 't1', report_id: 'r2' } },
    { event_id: 'e3', detail: [event.detail] }],
  reports: [{ report_id: 'r1' }, { report_id: 'r2' }],
}

describe('experiment evidence deep links', () => {
  it('resolves every linked record in the selected experiment', () => {
    expect(resolveExperimentEvidence(records, new URLSearchParams('trade=t1&event=e1&report=r1')))
      .toEqual({ trade, event, report: { report_id: 'r1' } })
  })

  it.each([
    'trade=t1&report=r2',
    'trade=t2&event=e1',
    'event=e1&report=r2',
    'trade=t1&event=e2&report=r1',
    'trade=t1&event=e3',
    'trade=missing&report=r1',
    'report=',
  ])('rejects the entire selection for %s', query => {
    expect(resolveExperimentEvidence(records, new URLSearchParams(query))).toBeNull()
  })

  it('clears old detail keys and retains unrelated query state when switching experiment', () => {
    const current = new URLSearchParams('experiment=old&event=e1&trade=t1&report=r1&module=local')
    const next = experimentQuery(current, { experiment: 'new' }, true)
    expect(Object.fromEntries(next)).toEqual({ experiment: 'new', module: 'local' })
    expect(current.get('trade')).toBe('t1')
  })
})

it('keeps missing valuation gaps and uses initial cash and observed peaks for drawdown', () => {
  const points = equityPoints([
    { at: 'a', total_assets: '750' },
    { at: 'b', total_assets: null },
    { at: 'c', total_assets: '1500' },
    { at: 'd', total_assets: '1125' },
  ], '1000')
  expect(points).toEqual([
    { time: 'a', equity: 750, drawdown: -25 },
    { time: 'b', equity: null, drawdown: null },
    { time: 'c', equity: 1500, drawdown: 0 },
    { time: 'd', equity: 1125, drawdown: -25 },
  ])
})
