"""Discovery can reacquire quotes; retired feeds cannot lock the active pool."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from stock_assistant.daytrade.contracts import instant
from stock_assistant.daytrade.stream import LivePaperSession

FIXTURE=Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json'


def sample():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))['events']


def ranking(at, *, event_id='rank', only='005930'):
    row=deepcopy(next(r for r in sample() if r['id']=='rankings'))
    row.update(id=event_id,at=at,sequence=100,received_monotonic=100)
    row['payload']['result']['rankedAt']=at
    if only:
        row['payload']['result']['rankings']=[r for r in row['payload']['result']['rankings'] if r['symbol']==only]
    return row


def quote(at, *, code='005930', event_id='quote', mono=101):
    row=deepcopy(next(r for r in sample() if r['kind']=='ws_frame' and r['frame']['topic']=='orderbook:kr:005930'))
    row.update(id=event_id,at=at,sequence=101,received_monotonic=mono)
    row['frame']['topic']='orderbook:kr:'+code
    row['frame']['data']['timestamp']=at
    return row


class StaleBookTrapTests(unittest.TestCase):
    def open_session(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        session=LivePaperSession(root.name,websocket_slot_available=True)
        self.addCleanup(session.close)
        session.add(sample()[0])
        return session

    def test_expired_cached_book_remains_watched_for_quote_reacquisition(self):
        session=self.open_session();at='2026-11-13T10:04:01+09:00'
        initial=quote(at,mono=1);initial['sequence']=1;session.add(initial)
        later=(instant(at)+timedelta(seconds=6)).isoformat()
        session.add(ranking(later))
        report=session.report();scan=report['scan_history'][-1]
        self.assertEqual([r['symbol'] for r in scan['selected']],['005930'])
        self.assertTrue(scan['selected'][0]['entry_blocked'])
        self.assertIn('QUOTE_UNAVAILABLE_WATCH_ONLY',scan['selected'][0]['selection_reasons'])
        self.assertFalse(scan['rejected'])

    def test_fresh_quote_promotes_watch_and_latest_scan_before_next_ranking(self):
        session=self.open_session();at='2026-11-13T10:04:01+09:00'
        session.add(ranking(at));self.assertTrue(session._runner.watches['005930']['entry_blocked'])
        session.add(quote(at))
        candidate=session._runner.watches['005930'];row=session.report()['scan_history'][-1]['selected'][0]
        for record in (candidate,row):
            self.assertFalse(record['entry_blocked'])
            self.assertGreater(Decimal(record['spread_bps']),0)
            self.assertIn('QUOTE_ELIGIBLE',record['selection_reasons'])
            self.assertNotIn('QUOTE_UNAVAILABLE_WATCH_ONLY',record['selection_reasons'])

    def test_bad_or_wide_book_never_promotes_watch(self):
        for problem in ('crossed','missing_time','wide'):
            with self.subTest(problem=problem):
                session=self.open_session();at='2026-11-13T10:04:01+09:00'
                session.add(ranking(at));session.add(quote(at))
                self.assertFalse(session._runner.watches['005930']['entry_blocked'])
                row=quote(at,event_id='bad-'+problem,mono=102);row['sequence']=102
                data=row['frame']['data']
                if problem=='crossed':data['bids'][0]['price']=data['asks'][0]['price']
                elif problem=='missing_time':data['timestamp']=None
                else:data['asks'][0]['price']='15000'
                session.add(row)
                self.assertTrue(session._runner.watches['005930']['entry_blocked'])
                self.assertTrue(session.report()['scan_history'][-1]['selected'][0]['entry_blocked'])

    def test_market_step_recovers_only_retired_feed_reasons(self):
        session=self.open_session();at='2026-11-13T10:04:01+09:00';session.add(ranking(at))
        for lock,reason in [('DATA_DEGRADED','orderbook:035420'),('DATA_DEGRADED','trade:035420'),
                            ('DATA_DEGRADED','candles:035420'),('DATA_DEGRADED','trade:005930'),
                            ('DATA_DEGRADED','HTTP:RANKINGS'),('RATE_LIMIT_DEGRADED','HTTP:RANKINGS'),
                            ('LOSS_LOCKED','DAILY_NET_EQUITY_LOSS')]:
            session._runner.emit('LOCK',{'lock':lock,'reason':reason},at)
        session.add(quote(at))
        reasons=session.ledger.state()['risk']['reasons']
        self.assertEqual(set(reasons['DATA_DEGRADED']),{'trade:005930','HTTP:RANKINGS'})
        self.assertIn('LOSS_LOCKED',reasons);self.assertIn('RATE_LIMIT_DEGRADED',reasons)

    def test_empty_watch_pool_does_not_assume_retired_feed_is_safe(self):
        session=self.open_session();at='2026-11-13T10:04:01+09:00'
        session._runner.emit('LOCK',{'lock':'DATA_DEGRADED','reason':'trade:035420'},at)
        session.add(quote(at,mono=1))
        self.assertIn('trade:035420',session.ledger.state()['risk']['reasons']['DATA_DEGRADED'])

    def test_retired_ws_and_candle_callbacks_do_not_enter_ledger_or_capture(self):
        session=self.open_session();at='2026-11-13T10:04:01+09:00';session.add(ranking(at))
        before=session.ledger.state_hash;captured=session.to_stream()
        stale=quote(at,code='035420',mono=1)
        candle=deepcopy(next(r for r in sample() if r['kind']=='candles'))
        candle.update(id='retired-bars',symbol='035420',sequence=102,received_monotonic=1)
        for row in (stale,candle):
            self.assertFalse(session.add(row))
            self.assertEqual(session.ledger.state_hash,before)
            self.assertEqual(session.to_stream(),captured)

    def test_held_symbol_remains_observed_after_removal_from_watch_pool(self):
        session=self.open_session()
        for row in sample()[1:]:
            session.add(row)
            if row['id']=='buy-profit':break
        self.assertTrue(session.ledger.state()['positions']['005930'])
        at=row['at'];rank=ranking(at,event_id='held-off-watch',only='000660');rank['received_monotonic']=200
        rank['risk_meta']['005930']={'halted':True}
        session.add(rank)
        self.assertNotIn('005930',session._runner.watches)
        bad=quote(at,event_id='held-crossed',mono=201)
        bad['frame']['data']['bids'][0]['price']=bad['frame']['data']['asks'][0]['price']
        self.assertTrue(session.add(bad))
        self.assertIn('orderbook:005930',session.ledger.state()['risk']['reasons']['DATA_DEGRADED'])


if __name__=='__main__':unittest.main()
