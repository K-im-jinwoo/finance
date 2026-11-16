"""Live quality recovery and checkpoint write amplification regression tests."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from stock_assistant.daytrade.stream import LivePaperSession

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from research_dashboard.daytrade_bridge import read_daytrade_report


def sample():
    return json.loads((ROOT/'engine/tests/fixtures/daytrade_observed_stream_sample.json').read_text(encoding='utf-8'))


class ContinuousTests(unittest.TestCase):
    def test_volume_refresh_updates_actual_watch_subscriptions(self):
        class Adapter:
            desired=set()
            def replace(self,topics,*,now):self.desired=set(topics)
        adapter=Adapter()
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,ws_adapter=adapter) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='rankings':break
            self.assertTrue(adapter.desired)
            row=deepcopy(next(r for r in sample()['events'] if r['kind']=='rankings' and r['ranking_type']=='MARKET_TRADING_AMOUNT'))
            row.update(id='volume-refresh',sequence=100,ranking_type='MARKET_TRADING_VOLUME')
            row['risk_meta']={code:{'halted':True} for code in ('005930','000660','035420')}
            s.add(row)
            self.assertFalse(s._runner.watches)
            self.assertEqual(adapter.desired,set())

    def test_good_quote_recovers_only_matching_quality_reason(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root,websocket_slot_available=True) as s:
            s.add(sample()['events'][0])
            at=sample()['events'][1]['at']
            for lock,reason in [('DATA_DEGRADED','orderbook:005930'),('DATA_DEGRADED','trade:000660'),
                                ('DATA_DEGRADED','HTTP:RANKINGS'),('RATE_LIMIT_DEGRADED','HTTP:RANKINGS'),
                                ('LOSS_LOCKED','DAILY_NET_EQUITY_LOSS')]:
                s._runner.emit('LOCK',{'lock':lock,'reason':reason},at)
            row=next(deepcopy(r) for r in sample()['events'] if r['kind']=='ws_frame' and r['frame']['topic']=='orderbook:kr:005930')
            s.add(row)
            reasons=s.ledger.state()['risk']['reasons']
            self.assertNotIn('orderbook:005930',reasons.get('DATA_DEGRADED',[]))
            self.assertIn('trade:000660',reasons['DATA_DEGRADED'])
            self.assertIn('HTTP:RANKINGS',reasons['DATA_DEGRADED'])
            self.assertIn('LOSS_LOCKED',reasons);self.assertIn('RATE_LIMIT_DEGRADED',reasons)

    def test_removed_symbol_recovers_but_held_symbol_and_bad_quote_do_not(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root,websocket_slot_available=True) as s:
            rows=sample()['events']
            for row in rows:
                s.add(row)
                if row['id']=='buy-profit':break
            self.assertTrue(s.ledger.state()['positions'].get('005930'))
            at=row['at']
            for reason in ('orderbook:005930','trade:035420','HTTP:RANKINGS'):
                s._runner.emit('LOCK',{'lock':'DATA_DEGRADED','reason':reason},at)
            ranking=deepcopy(next(r for r in rows if r['kind']=='rankings' and r['ranking_type']=='MARKET_TRADING_AMOUNT'))
            ranking['id']='remove-both';ranking['at']=at;ranking['sequence']=100
            ranking['risk_meta']={'005930':{'halted':True},'035420':{'halted':True}}
            s.add(ranking)
            reasons=s.ledger.state()['risk']['reasons']['DATA_DEGRADED']
            self.assertIn('orderbook:005930',reasons)
            self.assertNotIn('trade:035420',reasons)
            self.assertIn('HTTP:RANKINGS',reasons)
            bad=deepcopy(next(r for r in rows if r['kind']=='ws_frame' and r['frame']['topic']=='orderbook:kr:005930'))
            bad.update(id='still-crossed',at=at,sequence=101,received_monotonic=100)
            bad['frame']['data']['bids'][0]['price']=bad['frame']['data']['asks'][0]['price']
            s.add(bad)
            self.assertIn('orderbook:005930',s.ledger.state()['risk']['reasons']['DATA_DEGRADED'])

    def test_known_valuation_recovers_uncertain_mark(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root,websocket_slot_available=True) as s:
            row=sample()['events'][0];s.add(row)
            s._runner.emit('LOCK',{'lock':'DATA_DEGRADED','reason':'MARK_UNCERTAIN'},row['at'])
            s.on_clock(at=row['at'])
            self.assertNotIn('MARK_UNCERTAIN',s.ledger.state()['risk']['reasons'].get('DATA_DEGRADED',[]))

    def test_live_checkpoint_is_one_latest_row_and_hold_history_is_compact(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root,websocket_slot_available=True) as s:
            rows=sample()['events']
            for row in rows:
                # Missing candles/trades keep both selected names in HOLD, so
                # repeated quotes exercise actual identical HOLD compression.
                if row['kind']=='candles' or (row['kind']=='ws_frame' and row['frame']['topic'].startswith('trade:')):
                    continue
                s.add(row)
                if row['id']=='rankings':break
            quote=next(deepcopy(r) for r in rows if r['kind']=='ws_frame' and r['frame']['topic']=='orderbook:kr:005930')
            last=s.to_stream()['events'][-1]
            quote['at']=last['at']
            for i in range(120):
                quote.update(id=f'repeat-{i}',sequence=100+i,received_monotonic=100+i)
                s.add(quote)
            heavy=s.ledger.connection.execute("SELECT count(*) FROM events WHERE payload LIKE '%_bot_summary%'").fetchone()[0]
            self.assertEqual(heavy,0)
            self.assertEqual(s.ledger.connection.execute("SELECT count(*) FROM meta WHERE key='_bot_summary'").fetchone()[0],1)
            report=read_daytrade_report(root)['report']
            self.assertEqual(report,s.report())
            self.assertTrue(report['paper_ready'])
            self.assertTrue(any(row['action']=='HOLD' for row in report['signals']))
            self.assertLess(len(report['signals']),30)
            self.assertTrue(report['audit']['cash_identity'])


if __name__=='__main__':unittest.main()
