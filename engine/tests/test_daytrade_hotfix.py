"""Urgent live-loop regressions. Synthetic inputs only; no network or messaging."""
from copy import deepcopy
from datetime import timedelta
import json
import random
import tempfile
import unittest

from stock_assistant.daytrade.contracts import instant
from stock_assistant.daytrade.stream import LivePaperSession,provisional_quality
from stock_assistant.daytrade.daemon import open_or_resume_session,StickyAdapter,poll_websocket
from stock_assistant.daytrade.live_config import live_settings
from stock_assistant.daytrade.websocket import WebSocketAdapter
from test_daytrade_phase1 import sample
from test_daytrade_scanner_strategy import NOW,CODE
from daytrade_harness import FakeServer
from test_daytrade_live_candles import trade


def oversized_summary(runner):
    reject={'symbol':'000001','reason':'LOW_PRICE','reasons':['LOW_PRICE']}
    runner.meta['scan_history']=[{'at':NOW,'selected':[],'rejected':[dict(reject) for _ in range(200)]} for _ in range(100)]
    runner.meta['signals']=[{'symbol':f'{i%16:06d}','at':NOW,'action':'HOLD','score':0,
        'reasons':['SYNTHETIC'], 'exit_reason':None,'indicators':{'sample':i}} for i in range(4000)]
    runner.meta['decisions']=[{'at':NOW,'status':'SKIPPED','reason':'SYNTHETIC'} for _ in range(4000)]


class HotfixTests(unittest.TestCase):
    def test_live_summary_is_bounded_keeps_latest_symbols_and_rebuilds_indexes(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            oversized_summary(s._runner)
            s._runner.checkpoint(NOW,latest_only=True)
            raw=s.ledger.connection.execute("SELECT value FROM meta WHERE key='_bot_summary'").fetchone()[0]
            self.assertLess(len(raw.encode()),200000)
            self.assertLessEqual(len(s._runner.meta['scan_history']),20)
            self.assertLessEqual(len(s._runner.meta['signals']),200)
            for code in (f'{i:06d}' for i in range(16)):
                index=s._runner.last_signal[code]
                self.assertEqual(s._runner.meta['signals'][index]['indicators']['sample'],max(i for i in range(4000) if f'{i%16:06d}'==code))

    def test_offline_checkpoint_retains_full_history(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            s._runner.compact_live=False
            oversized_summary(s._runner)
            s._runner.checkpoint(NOW,latest_only=True)
            self.assertEqual(len(s._runner.meta['scan_history']),100)
            self.assertEqual(len(s._runner.meta['signals']),4000)

    def test_byte_budget_trim_preserves_latest_for_every_retained_symbol(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            s._runner.meta['signals']=[{'symbol':f'{i:06d}','at':NOW,'action':'HOLD','reasons':[]} for i in range(150)]
            s._runner.trim_live_history(signal_limit=50)
            self.assertEqual(len(s._runner.last_signal),150)
            self.assertLessEqual(len(s._runner.meta['signals']),200)

    def test_resume_trims_old_cache_and_preserves_every_ledger_event(self):
        with tempfile.TemporaryDirectory() as root:
            s=open_or_resume_session(root,'2026-11-13')
            s.on_session(normal_session_verified=True,at=NOW)
            oversized_summary(s._runner)
            s.set_meta('_bot_summary',s._runner.meta)
            old=list(s.ledger.connection.execute('SELECT id,fingerprint,payload FROM events ORDER BY sequence'))
            s.close()
            s=open_or_resume_session(root,'2026-11-13',live_settings=live_settings())
            try:
                self.assertLess(s.ledger.connection.execute("SELECT length(value) FROM meta WHERE key='_bot_summary'").fetchone()[0],200000)
                self.assertEqual(list(s.ledger.connection.execute('SELECT id,fingerprint,payload FROM events ORDER BY sequence')),old)
                self.assertEqual(s._checkpoint_interval,4)
            finally:s.close()

    def test_stale_ranking_keeps_subscriptions_and_blocks_entry_until_fresh(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='rankings':break
            adapter=StickyAdapter(lambda:None,rng=random.Random(1));adapter.session=s;s.ws_adapter=adapter
            adapter.replace(set(),now=10)
            desired=set(adapter.desired)
            before=set(s._runner.watches)
            old=next(deepcopy(r) for r in sample()['events'] if r['id']=='rankings')
            later=instant(old['at'])+timedelta(seconds=91)
            old.update(id='stale-rank',sequence=100,at=later.isoformat(),received_monotonic=200)
            s.add(old)
            self.assertEqual(set(s._runner.watches),before)
            self.assertEqual(adapter.desired,desired)
            self.assertTrue(all('STALE_RANKINGS' in c['issues'] for c in s._runner.watches.values()))
            self.assertEqual(s._runner.entry_block_reason(CODE,later),'STALE_RANKINGS')
            self.assertFalse(s._runner.pending(side='BUY'))
            for sequence,kind in enumerate(('MARKET_TRADING_AMOUNT','MARKET_TRADING_VOLUME'),101):
                fresh=deepcopy(old);fresh.update(id='fresh-'+kind,sequence=sequence,ranking_type=kind)
                fresh['payload']['result']['rankedAt']=later.isoformat()
                s.add(fresh)
            self.assertTrue(s._runner.watches)
            self.assertTrue(all('STALE_RANKINGS' not in c.get('issues',[]) for c in s._runner.watches.values()))

    def test_stale_ranking_does_not_preserve_explicitly_halted_candidate(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='rankings':break
            stale=deepcopy(row);stale.update(id='stale-halted',sequence=100,
                at=(instant(row['at'])+timedelta(seconds=91)).isoformat(),received_monotonic=200)
            stale['risk_meta']={CODE:{'halted':True}}
            s.add(stale)
            self.assertNotIn(CODE,s._runner.watches)
            self.assertIn('000660',s._runner.watches)

    def test_stale_union_missing_old_symbol_still_rechecks_new_safety_flags(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='rankings':break
            turnover=deepcopy(s._rankings['MARKET_TRADING_AMOUNT'])
            volume=deepcopy(s._rankings['MARKET_TRADING_VOLUME'])
            for value in (turnover,volume):
                value['rankings']=[r for r in value['rankings'] if r['symbol']!=CODE]
            at=(instant(row['at'])+timedelta(seconds=91)).isoformat()
            s._runner.step({'id':'missing-but-halted','at':at,'kind':'RANKINGS',
                'data':{'turnover':turnover,'volume':volume,'risk_meta':{CODE:{'halted':True}}}})
            self.assertNotIn(CODE,s._runner.watches)
            self.assertIn('000660',s._runner.watches)

    def test_burst_can_drain_more_than_32_frames_and_still_has_time_budget(self):
        server=FakeServer();seen=[]
        adapter=WebSocketAdapter(server.connect,rng=random.Random(1),on_message=seen.append)
        adapter.replace({'trade:kr:005930'},now=0);adapter.connect(now=0)
        for _ in range(600):server.sockets[0].push(trade(NOW))
        count=poll_websocket(adapter,lambda:1,budget_clock=lambda:0)
        self.assertEqual(count,512)
        self.assertEqual(len(seen),511)
        ticks=iter((0,.02,.04,.06,.08))
        count=poll_websocket(adapter,lambda:1,budget_clock=lambda:next(ticks))
        self.assertLessEqual(count,3)

    def test_live_parameters_merge_quality_and_keep_quote_age_five(self):
        from stock_assistant.daytrade.live_config import merge_live_settings
        settings=live_settings()
        self.assertEqual(settings['quality']['ranking_max_age_seconds'],90)
        self.assertEqual(settings['strategy_config']['min_bid_notional'],'5000000')
        merged=merge_live_settings({'quality':{'quote_max_age_seconds':5,'candle_max_age_seconds':90}},settings)
        self.assertEqual(merged['quality']['quote_max_age_seconds'],5)
        self.assertEqual(provisional_quality().ranking_max_age_seconds,45)

    def test_intent_and_scan_flush_inside_slow_checkpoint_interval(self):
        clock=[0]
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,
                checkpoint_interval_seconds=4,checkpoint_clock=lambda:clock[0]) as s:
            for row in sample()['events']:
                if row['kind']=='candles':continue
                s.add(row)
                if row['id']=='rankings':break
            self.assertEqual(s.ledger.meta['_bot_summary']['_checkpoint_sequence'],s._runner.sequence)
            row=next(deepcopy(r) for r in sample()['events'] if r['id']=='candles-005930')
            row.update(id='trigger-intent',sequence=100,received_monotonic=100)
            s.add(row)
            self.assertTrue(s._runner.pending(side='BUY'))
            self.assertEqual(s.ledger.meta['_bot_summary']['_checkpoint_sequence'],s._runner.sequence)


if __name__=='__main__':unittest.main()
