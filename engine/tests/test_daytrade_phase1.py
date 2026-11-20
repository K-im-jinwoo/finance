"""Approved Phase1 regression contracts. All inputs synthetic; no network/sending."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import queue
import random
import tempfile
import unittest

from stock_assistant.daytrade.contracts import instant
from stock_assistant.daytrade.scanner import scan_rankings
from stock_assistant.daytrade.stream import LivePaperSession, LiveCandleBuilder
from stock_assistant.daytrade.strategy import StrategyEngine
from test_daytrade_scanner_strategy import (NOW, CODE, ranking, scanner_config,
    strategy_config, ready_engine, quote, candle_event, risk, observation)


def stock(**updates):
    return dict(name='테스트전자', market='KOSDAQ', securityType='STOCK', isCommonShare=True,
        leverageFactor=None, status='ACTIVE', sharesOutstanding='10000000',
        koreanMarketDetail={'liquidationTrading':False, 'krxTradingSuspended':False,
            'nxtTradingSuspended':None}, **updates)


def sample():
    return json.loads((Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json').read_text(encoding='utf-8'))


class ScreeningTests(unittest.TestCase):
    def scan(self, meta=None, price='10000', **config):
        return scan_rankings(ranking([(CODE, price, '2000000')]), now=NOW,
            config=scanner_config(require_common_stock=True, **config), risk_meta={CODE:meta} if meta else {})

    def test_common_stock_and_price_cap_edges(self):
        for change in ({'securityType':'ETF'}, {'isCommonShare':False}, {'leverageFactor':'2'},
                       {'market':'KR_ETC'}, {'status':'DELISTED'}, {'name':'테스트제1호스팩'},
                       {'securityType':'REIT'}):
            value=stock();value.update(change)
            self.assertFalse(self.scan(value).selected)
        self.assertFalse(self.scan().selected)
        self.assertTrue(self.scan(stock(), price='200000', max_price='200000').selected)
        self.assertIn('HIGH_PRICE', self.scan(stock(), price='200001', max_price='200000').rejected[0]['reasons'])

    def test_market_cap_missing_boundaries_and_suspended_flags(self):
        self.assertTrue(self.scan(stock(), min_market_cap='100000000000', max_market_cap='100000000000').selected)
        self.assertIn('LOW_MARKET_CAP', self.scan(stock(), min_market_cap='100000000001').rejected[0]['reasons'])
        self.assertIn('HIGH_MARKET_CAP', self.scan(stock(), max_market_cap='99999999999').rejected[0]['reasons'])
        value=stock();value.pop('sharesOutstanding')
        self.assertFalse(self.scan(value, max_market_cap='100000000000').selected)
        for key in ('liquidationTrading','krxTradingSuspended','nxtTradingSuspended'):
            value=stock();value['koreanMarketDetail'][key]=True
            self.assertFalse(self.scan(value).selected)
        value=stock();value['koreanMarketDetail'].pop('nxtTradingSuspended')
        value['koreanMarketDetail']['nxtSupported']=False
        self.assertTrue(self.scan(value).selected)

    def test_metadata_expiry_shortens_candidate_and_expired_rejects(self):
        value=stock();value.update(metadata_status='VERIFIED', metadata_valid_until=(instant(NOW)+timedelta(seconds=3)).isoformat())
        result=self.scan(value)
        self.assertEqual(instant(result.selected[0]['valid_until']),instant(NOW)+timedelta(seconds=3))
        value['metadata_valid_until']=(instant(NOW)-timedelta(seconds=1)).isoformat()
        self.assertFalse(self.scan(value).selected)

    def test_live_stream_preserves_stock_reference_metadata(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root,websocket_slot_available=True,
                scanner_config={'require_common_stock':True}) as s:
            for row in sample()['events']:
                if row['kind']=='rankings':
                    row=deepcopy(row);row['risk_meta']={c:stock() for c in ('005930','000660','035420')}
                s.add(row)
                if row['id']=='rankings':break
            self.assertTrue(s._runner.watches)
            self.assertEqual(s._risk_metadata[CODE]['securityType'],'STOCK')


class StrategyTests(unittest.TestCase):
    def test_subscription_warmup_and_optional_settings_validation(self):
        engine=ready_engine()
        future=(instant(NOW)+timedelta(seconds=1)).isoformat()
        self.assertIn('SUBSCRIPTION_WARMUP',engine.evaluate(CODE,NOW,risk(),{}, {'warmup_until':future}).reasons)
        for config in ({'max_price':'-1'},{'require_common_stock':'true'},
                {'min_market_cap':'200','max_market_cap':'100'}):
            with self.assertRaises(ValueError):scanner_config(**config)
        for config in ({'min_bid_notional':'0'},{'entry_start_hhmm':'25:00'},
                {'min_hold_seconds_for_weak_exit':True}):
            with self.assertRaises(ValueError):strategy_config(**config)

    def test_bid_notional_gate_and_diagnostics(self):
        engine=ready_engine(config=strategy_config(min_bid_volume='1',min_bid_notional='20000000'),
                            book=quote(volume='1000'))
        signal=engine.evaluate(CODE,NOW,risk(),{})
        self.assertEqual(signal.action,'HOLD')
        self.assertEqual(signal.indicators['best_bid_notional'],109000)
        self.assertEqual(signal.indicators['ask_bid_volume_ratio'],1)
        engine=ready_engine(config=strategy_config(min_bid_volume='1',min_bid_notional='20000000'),
                            book=quote(volume='200000'))
        self.assertEqual(engine.evaluate(CODE,NOW,risk(),{}).action,'BUY')

    def test_warmup_blocks_only_buy_and_hard_stop_bypasses_weak_grace(self):
        config=strategy_config(entry_start_hhmm='10:05',min_hold_seconds_for_weak_exit=120)
        engine=ready_engine(config=config)
        signal=engine.evaluate(CODE,NOW,risk(),{})
        self.assertIn('ENTRY_WINDOW_WARMUP',signal.reasons)
        position={CODE:{'quantity':1,'average_price':'200','entry_at':NOW}}
        self.assertEqual(engine.evaluate(CODE,NOW,risk(),position).exit_reason,'STOP_LOSS')
        forced=risk('2026-11-13T15:15:00+09:00')
        self.assertEqual(engine.evaluate(CODE,'2026-11-13T15:15:00+09:00',forced,position).exit_reason,'TIME_EXIT')

    def test_weak_exit_requires_elapsed_hold_but_take_profit_is_immediate(self):
        engine=ready_engine(config=strategy_config(min_hold_seconds_for_weak_exit=120),
            candles=candle_event(closes=('110','110','110','109')),book=quote(bid='109',ask='110'))
        p={CODE:{'quantity':1,'average_price':'110','entry_at':NOW}}
        self.assertNotEqual(engine.evaluate(CODE,NOW,risk(),p).exit_reason,'MOMENTUM_WEAK')
        p[CODE]['entry_at']=(instant(NOW)-timedelta(seconds=120)).isoformat()
        self.assertEqual(engine.evaluate(CODE,NOW,risk(),p).exit_reason,'MOMENTUM_WEAK')
        p[CODE]['average_price']='100'
        self.assertEqual(engine.evaluate(CODE,NOW,risk(),p).exit_reason,'TAKE_PROFIT')

    def test_orderbook_ratio_confirmed_breakout_weak_gap_and_trailing_profit(self):
        for bad in ({'max_ask_bid_volume_ratio':'0'},{'breakout_lookback_bars':0},
                {'weak_exit_min_vwap_gap_bps':'-1'},{'trailing_stop_activation_bps':'100'},
                {'trailing_stop_activation_bps':'50','trailing_stop_retrace_bps':'50'}):
            with self.assertRaises(ValueError):strategy_config(**bad)
        heavy_ask=observation('orderbook',{'currency':'KRW','timestamp':NOW,
            'bids':[{'price':'109','volume':'1000'}],'asks':[{'price':'110','volume':'2000'}]})
        cfg=strategy_config(max_ask_bid_volume_ratio='1.5',breakout_lookback_bars=3,
            weak_exit_min_vwap_gap_bps='50',trailing_stop_activation_bps='90',trailing_stop_retrace_bps='40')
        self.assertIn('MISSING_BID_DEPTH',ready_engine(config=cfg,book=heavy_ask).evaluate(CODE,NOW,risk(),{}).reasons)
        bullish_bars=observation('candles',{'candles':[
            {'currency':'KRW','timestamp':'2026-11-13T10:01:00+09:00','openPrice':'100','highPrice':'101','lowPrice':'99','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:02:00+09:00','openPrice':'100','highPrice':'101','lowPrice':'99','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:03:00+09:00','openPrice':'100','highPrice':'101','lowPrice':'99','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:04:00+09:00','openPrice':'100','highPrice':'111','lowPrice':'100','closePrice':'110','volume':'400'}]})
        engine=ready_engine(config=cfg,candles=bullish_bars,book=quote(bid='109',ask='110',volume='1000'))
        self.assertEqual(engine.evaluate(CODE,NOW,risk(),{}).action,'BUY')
        # Tiny 1-tick dip below VWAP is held when weak_exit_min_vwap_gap_bps=50.
        dip_bars=candle_event(closes=('110','110','110','109.8'))
        dip_engine=ready_engine(config=cfg,candles=dip_bars,book=quote(bid='109.8',ask='110'))
        pos={CODE:{'quantity':1,'average_price':'110','entry_at':(instant(NOW)-timedelta(seconds=200)).isoformat()}}
        self.assertEqual(dip_engine.evaluate(CODE,NOW,risk(),pos).action,'HOLD')
        # Post-entry bar high (+200bps) followed by retrace to +100bps triggers trailing TAKE_PROFIT.
        trail_bars=observation('candles',{'candles':[
            {'currency':'KRW','timestamp':'2026-11-13T10:01:00+09:00','openPrice':'100','highPrice':'100','lowPrice':'100','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:02:00+09:00','openPrice':'100','highPrice':'100','lowPrice':'100','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:03:00+09:00','openPrice':'100','highPrice':'100','lowPrice':'100','closePrice':'100','volume':'100'},
            {'currency':'KRW','timestamp':'2026-11-13T10:04:00+09:00','openPrice':'100','highPrice':'102','lowPrice':'100','closePrice':'101','volume':'200'}]})
        trail_engine=ready_engine(config=cfg,candles=trail_bars,book=quote(bid='101',ask='101.5'))
        trail_pos={CODE:{'quantity':1,'average_price':'100','entry_at':'2026-11-13T10:03:10+09:00'}}
        sold=trail_engine.evaluate(CODE,NOW,risk(),trail_pos)
        self.assertEqual((sold.action,sold.exit_reason),('SELL','TAKE_PROFIT'))
        self.assertIn('TRAILING_STOP',sold.reasons)


class LifecycleTests(unittest.TestCase):
    def test_live_checkpoints_are_bounded_but_fills_flush_immediately(self):
        clock=[0]
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,
                checkpoint_interval_seconds=1,checkpoint_clock=lambda:clock[0]) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='buy-profit':break
            self.assertTrue(s.ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0])
            seq=s.ledger.meta['_bot_summary']['_checkpoint_sequence']
            self.assertEqual(seq,s._runner.sequence)
            for _ in range(10):s.on_clock(at=row['at'])
            self.assertEqual(s.ledger.meta['_bot_summary']['_checkpoint_sequence'],seq)
            clock[0]=1
            s.on_clock(at=row['at'])
            self.assertEqual(s.ledger.meta['_bot_summary']['_checkpoint_sequence'],s._runner.sequence)

    def test_websocket_burst_drains_ack_and_frames_in_same_cycle(self):
        from stock_assistant.daytrade.daemon import poll_websocket
        from stock_assistant.daytrade.websocket import WebSocketAdapter
        from daytrade_harness import FakeServer
        from test_daytrade_live_candles import trade
        server=FakeServer();seen=[]
        adapter=WebSocketAdapter(server.connect,rng=random.Random(1),on_message=seen.append)
        adapter.replace({'trade:kr:005930'},now=0);adapter.connect(now=0)
        for _ in range(10):server.sockets[0].push(trade(NOW))
        count=poll_websocket(adapter,lambda:1)
        self.assertEqual(count,11)
        self.assertEqual(len(seen),10)
        self.assertEqual(adapter.status,'LIVE')

    def test_malformed_reference_payload_fails_closed(self):
        from stock_assistant.daytrade.metadata import StockMetadataCache
        from stock_assistant.daytrade.http import HttpResult
        cache=StockMetadataCache()
        for value in ([],None,{'result':[{'symbol':CODE,'name':123}]}):
            cache.accept([CODE],HttpResult(200,value,{}),at=NOW)
            self.assertEqual(cache.snapshot([CODE],at=NOW)[CODE]['metadata_status'],'UNAVAILABLE')
        value=stock();value['market']=[]
        result=scan_rankings(ranking([(CODE,'10000','2000000')]),now=NOW,
            config=scanner_config(require_common_stock=True),risk_meta={CODE:value})
        self.assertFalse(result.selected)

    def test_ws_eviction_cancels_selected_buy_and_releases_reserved_entry(self):
        from types import SimpleNamespace
        from stock_assistant.daytrade.daemon import StickyAdapter
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True) as s:
            for row in sample()['events']:
                s.add(row)
                if row['id']=='rankings':break
            self.assertTrue(s._runner.pending(code=CODE,side='BUY'))
            state=s.ledger.state();state['positions']={'999999':1}
            fake=SimpleNamespace(ledger=SimpleNamespace(state=lambda:state),_runner=s._runner)
            adapter=StickyAdapter(lambda:None,rng=random.Random(1));adapter.session=fake
            codes=[f'{i:06d}' for i in range(1,7)]+[CODE,'008888']
            candidate=s._runner.watches[CODE]
            s._runner.watches={c:{**candidate,'symbol':c} for c in codes}
            adapter.replace(set(),now=0)
            self.assertTrue(s._runner.pending(code=CODE,side='BUY'))
            s._runner.watches={c:{**candidate,'symbol':c} for c in ['111111']+codes[:-1]}
            adapter.replace(set(),now=121)
            self.assertNotIn(CODE,adapter.sticky)
            self.assertIn(CODE,s._runner.watches)
            self.assertFalse(s._runner.pending(code=CODE,side='BUY'))
            self.assertEqual(s._runner.entry_history(NOW)['used_entries'],0)
            self.assertEqual(s._runner.entry_block_reason(CODE,NOW),'NOT_SUBSCRIBED')

    def test_websocket_library_peer_close_is_classified_as_network_disconnect(self):
        from stock_assistant.daytrade.daemon import TrackedSocket
        error_type=type('ConnectionClosedError',(Exception,),{'__module__':'websockets.exceptions'})
        class Raw:
            def recv(self,**_):raise error_type('must not log upstream text')
            def send(self,_):raise error_type('peer closed')
            def close(self):raise error_type('already closed')
        stats={'active_sockets':1};sock=TrackedSocket(Raw(),stats)
        with self.assertRaises(ConnectionError):sock.recv(timeout=.01)
        with self.assertRaises(ConnectionError):sock.send('PING')
        sock.close()
        self.assertEqual(stats['active_sockets'],0)

    def test_shutdown_after_peer_close_still_preserves_ledger_and_report(self):
        from stock_assistant.daytrade.daemon import open_or_resume_session,finish_session
        class Socket:
            def send(self,_):raise ConnectionError('peer closed')
        class Adapter:
            socket=Socket();closed=False
            def close(self):self.closed=True
        with tempfile.TemporaryDirectory() as root:
            s=open_or_resume_session(root,'2026-11-13')
            s.on_session(normal_session_verified=True,at=NOW)
            adapter=Adapter()
            finish_session(s,adapter,at=NOW,end='2026-11-13T15:30:00+09:00')
            self.assertTrue(adapter.closed)
            self.assertTrue((Path(root)/'daily/2026-11-13/report.json').exists())

    def test_partial_fills_count_once(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,max_daily_entries=2) as s:
            for raw in sample()['events']:
                row=deepcopy(raw)
                if row['kind']=='ws_frame' and row['frame']['topic']=='orderbook:kr:005930':
                    row['frame']['data']['asks'][0]['volume']='20'
                s.add(row)
                if row['id']=='take-profit-signal':
                    buys=s.ledger.connection.execute("SELECT intent_id FROM paper_fills WHERE side='BUY' AND symbol='005930'").fetchall()
                    self.assertGreater(len(buys),1)
                    self.assertEqual(len(set(buys)),1)
                    self.assertEqual(s._runner.entry_history(row['at'])['filled_entries'],2)
                    break

    def test_sell_bypasses_daily_entry_limit(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,max_daily_entries=1) as s:
            for raw in sample()['events']:s.add(raw)
            self.assertTrue(s.ledger.connection.execute("SELECT count(*) FROM paper_fills WHERE side='SELL'").fetchone()[0])
            self.assertEqual(s._runner.entry_history(raw['at'])['filled_entries'],1)

    def test_pending_buy_rechecks_notional_at_fill(self):
        with tempfile.TemporaryDirectory() as root,LivePaperSession(root,websocket_slot_available=True,
                strategy_config={'min_bid_volume':'1','min_bid_notional':'1000000'}) as s:
            for row in sample()['events']:
                row=deepcopy(row)
                if row['id']=='buy-profit':row['frame']['data']['bids'][0]['volume']='1'
                s.add(row)
                if row['id']=='buy-profit':break
            self.assertFalse(s.ledger.connection.execute("SELECT count(*) FROM paper_fills WHERE symbol='005930'").fetchone()[0])
            self.assertTrue(any(d['reason']=='BUY_FILL_QUOTE_DEPTH' for d in s._runner.meta['decisions']))

    def test_live_collector_fetches_reference_before_filtered_subscription(self):
        import threading
        from stock_assistant.daytrade.daemon import Collector,open_or_resume_session
        from stock_assistant.daytrade.live_config import live_settings
        from stock_assistant.daytrade.http import HttpResult
        from unittest.mock import patch
        now=[instant('2026-11-13T15:24:00+09:00')]
        checked=[False]
        class Transport:
            statuses={'200':0};requests=[]
            def __call__(self,path,query):
                self.statuses['200']+=1;self.requests.append({'path':path,'http_status':200})
                if path=='/api/v1/stocks':
                    checked[0]=True
                    rows=[]
                    for code in query['symbols'].split(','):
                        value=stock();value['symbol']=code
                        if code=='069500':value['securityType']='ETF'
                        rows.append(value)
                    return HttpResult(200,{'result':rows},{})
                rows=ranking([('123456','10000','300000000'),('069500','10000','400000000'),('005930','250000','200000000')],at=now[0].isoformat())
                return HttpResult(200,{'result':{k:v for k,v in rows.items() if k!='scope'}},{})
        class Socket:
            ack=None
            def send(self,raw):
                if raw in ('[]','PING'):return
                self_test.assertTrue(checked[0])
                rows=json.loads(raw)
                topics=[f"{r['type']}:{c}" for r in rows[1:] for c in r['codes']]
                self_test.assertEqual(set(t.rsplit(':',1)[1] for t in topics),{'123456'})
                self.ack={'type':'subscriptions','id':rows[0]['id'],'subscribed':topics,'rejected':[]}
            def recv(self,**_):
                if self.ack:
                    value,self.ack=self.ack,None;return json.dumps(value)
                now[0]+=timedelta(seconds=1)
                return json.dumps({'type':'message','topic':'trade:kr:123456','data':{
                    'timestamp':now[0].isoformat(),'currency':'KRW','price':'10000','volume':'1000'}})
            def close(self):pass
        self_test=self
        stop=threading.Event();timeout=threading.Timer(8,stop.set);timeout.start()
        try:
            with tempfile.TemporaryDirectory() as root:
                s=open_or_resume_session(root,'2026-11-13',live_settings=live_settings())
                s.on_session(normal_session_verified=True,at=now[0])
                collector=Collector(Transport(),lambda:'fake',None,stop,root,clock=lambda:now[0])
                with patch('stock_assistant.daytrade.daemon.runtime_socket_factory',return_value=lambda:Socket()):
                    result=collector.run(s,instant('2026-11-13T15:30:00+09:00'))
                self.assertEqual(result['status'],'CLOSED')
                self.assertEqual(set(s._runner.watches),{'123456'})
                self.assertTrue(s._closed)
        finally:timeout.cancel()

    def test_reservations_partial_fills_cooldown_and_restart_settings(self):
        from stock_assistant.daytrade.daemon import open_or_resume_session
        from stock_assistant.daytrade.live_config import live_settings
        with tempfile.TemporaryDirectory() as root:
            s=open_or_resume_session(root,'2026-11-13')
            for row in sample()['events']:
                if row['kind']=='end_session':break
                s.add(row)
            before=s.ledger.state();count=s.ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0]
            s.close()
            s=open_or_resume_session(root,'2026-11-13',live_settings=live_settings())
            try:
                self.assertEqual(s.ledger.state()['cash'],before['cash'])
                self.assertEqual(s.ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0],count)
                self.assertTrue(s._runner.config.scanner_config.require_common_stock)
                self.assertEqual(s._runner.config.max_daily_entries,5)
                history=s._runner.entry_history('2026-11-13T15:16:00+09:00')
                self.assertEqual(history['filled_entries'],2)
                self.assertIn(CODE,history['last_flat_at'])
                self.assertEqual(s._runner.entry_block_reason(CODE,'2026-11-13T10:05:00+09:00'),'REENTRY_COOLDOWN')
                self.assertFalse(s._runner.watches)
            finally:s.close()

    def test_pending_reservations_count_once_release_and_restore(self):
        from stock_assistant.daytrade.daemon import open_or_resume_session
        with tempfile.TemporaryDirectory() as root:
            s=open_or_resume_session(root,'2026-11-13',live_settings={'max_daily_entries':1})
            try:
                for row in sample()['events']:
                    s.add(row)
                    if row['id']=='rankings':break
                pending=s._runner.pending(side='BUY')
                self.assertEqual(len(pending),1)
                self.assertEqual(s._runner.entry_block_reason('035420',row['at']),'DAILY_ENTRY_LIMIT')
                s._runner.inactive.update(pending)
                self.assertIsNone(s._runner.entry_block_reason('035420',row['at']))
            finally:s.close()

    def test_rotation_protects_held_names_minimum_stay_and_two_replacements(self):
        from stock_assistant.daytrade.daemon import StickyAdapter
        a=StickyAdapter(lambda:None,rng=random.Random(1),min_stay_seconds=120,max_replacements=2)
        initial=[f'{i:06d}' for i in range(8)]
        a.ranked=initial;a.replace({f'trade:kr:{c}' for c in initial},now=0)
        a.held=['999999'];a.ranked=[f'{i:06d}' for i in range(20,28)]
        a.replace(set(),now=10)
        self.assertIn('999999',a.sticky)
        self.assertEqual(len(set(initial)-set(a.sticky)),1)
        before=set(a.sticky)
        a.replace(set(),now=121)
        self.assertLessEqual(len(set(a.sticky)-before),2)
        self.assertTrue(set(a.sticky)&set(a.ranked))
        self.assertEqual(len(a.desired),16)

    def test_metadata_cache_ttl_fail_closed_and_nonblocking_full_queue(self):
        from stock_assistant.daytrade.metadata import StockMetadataCache
        from stock_assistant.daytrade.http import HttpResult
        from stock_assistant.daytrade.daemon import enqueue_job
        cache=StockMetadataCache(ttl_seconds=300)
        self.assertEqual(cache.needed([CODE],at=NOW),[CODE])
        cache.pending.add(CODE)
        cache.accept([CODE],HttpResult(200,{'result':[{'symbol':CODE,**stock()}]},{}),at=NOW)
        self.assertEqual(cache.snapshot([CODE],at=NOW)[CODE]['metadata_status'],'VERIFIED')
        later=instant(NOW)+timedelta(seconds=301)
        self.assertEqual(cache.needed([CODE],at=later),[CODE])
        cache.accept([CODE],None,at=later)
        self.assertNotEqual(cache.snapshot([CODE],at=later)[CODE]['metadata_status'],'VERIFIED')
        self.assertEqual(cache.needed([CODE],at=later),[])
        jobs=queue.Queue(maxsize=1);jobs.put(('existing',{}))
        self.assertFalse(enqueue_job(jobs,('new',{})))


if __name__=='__main__':unittest.main()
