"""Observed WS aggregation, coalescing and point-in-time boundaries; no network."""
from copy import deepcopy
from decimal import Decimal
import importlib
import tempfile
import unittest


def trade(at,price='10000',volume='1',code='005930'):
    return {'type':'message','topic':'trade:kr:'+code,'data':{'timestamp':at,'currency':'KRW','price':price,'volume':volume}}


def book(at,bid='9990',ask='10000',code='005930'):
    return {'type':'message','topic':'orderbook:kr:'+code,'data':{'timestamp':at,'currency':'KRW',
        'bids':[{'price':bid,'volume':'1000'}],'asks':[{'price':ask,'volume':'1000'}]}}


class LiveCandleTests(unittest.TestCase):
    def module(self):
        m=importlib.import_module('stock_assistant.daytrade.stream')
        self.assertTrue(hasattr(m,'LiveCandleBuilder'),'WS candle builder missing')
        self.assertTrue(hasattr(m,'LiveFrameBuffer'),'bounded latest-frame buffer missing')
        return m

    def test_boundary_ohlcv_out_of_order_and_no_future_bar(self):
        b=self.module().LiveCandleBuilder()
        b.set_active({'005930'},at='2026-11-16T11:10:00+09:00')
        for stamp,p,v in [('30','11000','2'),('00','10000','3'),('59.999','10500','4')]:
            b.update(trade('2026-11-16T11:10:'+stamp+'+09:00',p,v),at='2026-11-16T11:10:59.999+09:00')
        self.assertEqual(b.advance(at='2026-11-16T11:10:59.999+09:00'),{})
        b.update(trade('2026-11-16T11:11:00+09:00','12000','5'),at='2026-11-16T11:11:00+09:00')
        bars=b.advance(at='2026-11-16T11:11:00+09:00')['005930']
        self.assertEqual(len(bars),1)
        self.assertEqual([bars[0][k] for k in ('openPrice','highPrice','lowPrice','closePrice','volume')],['10000','11000','10000','10500','9'])
        self.assertEqual(bars[0]['timestamp'],'2026-11-16T02:11:00+00:00')

    def test_active_no_trade_flat_bars_and_twenty_bar_retention(self):
        b=self.module().LiveCandleBuilder();b.set_active({'005930'},at='2026-11-16T11:10:00+09:00')
        b.update(trade('2026-11-16T11:10:30+09:00','10000','2'),at='2026-11-16T11:10:30+09:00')
        bars=b.advance(at='2026-11-16T11:14:00+09:00')['005930']
        self.assertEqual(len(bars),4);self.assertEqual([x['volume'] for x in bars],['2','0','0','0'])
        self.assertTrue(all(x['openPrice']==x['closePrice']=='10000' for x in bars))
        self.assertEqual(len(b.advance(at='2026-11-16T11:40:00+09:00')['005930']),20)

    def test_partial_start_skipped_and_unsubscribed_gap_not_invented(self):
        b=self.module().LiveCandleBuilder();b.set_active({'005930'},at='2026-11-16T11:10:23+09:00')
        b.update(trade('2026-11-16T11:10:25+09:00'),at='2026-11-16T11:10:25+09:00')
        self.assertEqual(b.advance(at='2026-11-16T11:11:00+09:00'),{})
        self.assertEqual(len(b.advance(at='2026-11-16T11:12:00+09:00')['005930']),1)
        b.set_active(set(),at='2026-11-16T11:12:01+09:00')
        self.assertEqual(b.advance(at='2026-11-16T11:15:00+09:00'),{})
        b.set_active({'005930'},at='2026-11-16T11:15:00+09:00')
        b.update(trade('2026-11-16T11:15:30+09:00','11000'),at='2026-11-16T11:15:30+09:00')
        bars=b.advance(at='2026-11-16T11:16:00+09:00')['005930']
        self.assertEqual(len(bars),2)
        self.assertEqual([x['timestamp'] for x in bars],['2026-11-16T02:12:00+00:00','2026-11-16T02:16:00+00:00'])

    def test_grace_immutable_closed_bar_and_new_day_reset(self):
        b=self.module().LiveCandleBuilder(grace_seconds=1)
        b.set_active({'005930'},at='2026-11-16T11:10:00+09:00')
        b.update(trade('2026-11-16T11:10:59.9+09:00',volume='2'),at='2026-11-16T11:11:00.5+09:00')
        self.assertEqual(b.advance(at='2026-11-16T11:11:00.5+09:00'),{})
        closed=b.advance(at='2026-11-16T11:11:01+09:00')['005930']
        self.assertFalse(b.update(trade('2026-11-16T11:10:59.9+09:00',volume='3'),at='2026-11-16T11:11:02+09:00'))
        self.assertEqual(b.history('005930'),closed)
        b.set_active({'005930'},at='2026-11-17T09:00:00+09:00')
        self.assertEqual(b.history('005930'),[])
        with self.assertRaises(ValueError):
            row=trade('2026-11-17T09:00:01+09:00');row['data']['currency']='USD';b.update(row,at='2026-11-17T09:00:01+09:00')

    def test_all_trade_volume_survives_latest_price_coalescing(self):
        m=self.module();b=m.LiveCandleBuilder();b.set_active({'005930'},at='2026-11-16T11:10:00+09:00')
        f=m.LiveFrameBuffer(b)
        for i in range(100):f.push(trade(f'2026-11-16T11:10:00.{i:03}+09:00',str(10000+i),'2'),at='2026-11-16T11:10:00.5+09:00')
        rows=f.drain(at='2026-11-16T11:10:01+09:00',monotonic=1)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['data']['price'],'10099')
        self.assertEqual(b.advance(at='2026-11-16T11:11:00+09:00')['005930'][0]['volume'],'200')

    def test_crossed_empty_stale_filter_and_pending_book_priority(self):
        m=self.module();f=m.LiveFrameBuffer(m.LiveCandleBuilder())
        at='2026-11-16T11:10:00+09:00'
        f.push(book(at),at=at);f.push(book(at,bid='10000',ask='10000'),at=at)
        empty=book(at);empty['data']['bids']=[];f.push(empty,at=at)
        self.assertEqual(len(f.drain(at=at,monotonic=1)),1)
        f.push(book('2026-11-16T11:10:00.1+09:00'),at='2026-11-16T11:10:00.1+09:00')
        self.assertEqual(f.drain(at='2026-11-16T11:10:00.1+09:00',monotonic=1.1),[])
        self.assertEqual(len(f.drain(at='2026-11-16T11:10:00.1+09:00',monotonic=1.1,pending_symbols={'005930'})),1)
        f.push(book(at),at=at)
        self.assertEqual(f.drain(at='2026-11-16T11:10:03.6+09:00',monotonic=5),[])
        self.assertGreaterEqual(f.counts['CROSSED_ORDERBOOK'],1);self.assertGreaterEqual(f.counts['EMPTY_ORDERBOOK'],1)

    def test_price_timestamp_never_rewinds_after_an_emission(self):
        m=self.module();f=m.LiveFrameBuffer(m.LiveCandleBuilder())
        f.push(book('2026-11-16T11:10:01+09:00'),at='2026-11-16T11:10:01+09:00')
        self.assertEqual(len(f.drain(at='2026-11-16T11:10:01+09:00',monotonic=1)),1)
        f.push(book('2026-11-16T11:10:00.9+09:00'),at='2026-11-16T11:10:01.5+09:00')
        self.assertEqual(f.drain(at='2026-11-16T11:10:01.5+09:00',monotonic=1.5),[])

    def test_four_ws_bars_reach_existing_numeric_indicators(self):
        m=self.module();b=m.LiveCandleBuilder();b.set_active({'005930'},at='2026-11-16T11:10:00+09:00')
        for i in range(4):b.update(trade(f'2026-11-16T11:{10+i}:10+09:00',str(10000+100*i),str(100*(i+1))),at=f'2026-11-16T11:{10+i}:10+09:00')
        bars=b.advance(at='2026-11-16T11:14:01+09:00')['005930'];at='2026-11-16T11:14:01+09:00'
        with tempfile.TemporaryDirectory() as root,m.LivePaperSession(root,websocket_slot_available=True) as s:
            s.on_session(at=at,normal_session_verified=True)
            s.on_frame(book(at,'10390','10400'),at=at,received_monotonic=1)
            s.on_frame(trade(at,'10400'),at=at,received_monotonic=2)
            s.add({'kind':'candles','symbol':'005930','source':'TOSS_WS_COMPLETED_1M','at':at,'received_monotonic':3,'payload':{'result':{'candles':bars}}})
            s.on_rest('/api/v1/rankings',{'status':'OK','payload':{'result':{'rankedAt':at,'rankings':[{'rank':1,'symbol':'005930','currency':'KRW','price':{'lastPrice':'10400'},'tradingAmount':'10000000000','tradingVolume':'1000000'}]}}},at=at,received_monotonic=4)
            signal=s.report()['signals'][-1]
            self.assertIsNotNone(signal['indicators']['bar_typical_vwap_approx'])
            self.assertIsNotNone(signal['indicators']['volume_spike_ratio'])
            self.assertEqual(Decimal(signal['indicators']['volume_spike_ratio']),Decimal('2'))
            sources=[__import__('json').loads(r[0])['source'] for r in s.ledger.connection.execute('SELECT payload FROM observations')]
            self.assertIn('TOSS_WS_COMPLETED_1M',sources)


if __name__=='__main__':unittest.main()
