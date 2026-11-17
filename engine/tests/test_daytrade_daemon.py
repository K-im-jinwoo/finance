"""Offline service lifecycle and real ledger restart tests; no external sending."""
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stock_assistant.daytrade.contracts import KST


class DaemonTests(unittest.TestCase):
    def module(self):
        return importlib.import_module('stock_assistant.daytrade.daemon')

    def test_calendar_skip_creates_no_ledger_or_socket(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            result = m.run_trading_day(root, transport=lambda *_: {'result': {'today': {
                'date': '2026-11-17', 'integrated': None}}},
                clock=lambda: datetime(2026, 11, 17, 8, 55, tzinfo=KST),
                run_session=lambda *_: self.fail('holiday session'))
            self.assertEqual(result['status'], 'MARKET_HOLIDAY_OR_NON_STANDARD_SESSION')
            self.assertFalse((Path(root)/'daily').exists())

    def test_preopen_waits_until_verified_normal_open(self):
        m = self.module()
        current = [datetime(2026, 11, 17, 8, 55, tzinfo=KST)]
        def sleep(seconds):
            current[0] += timedelta(seconds=seconds)
        def run(session, end):
            self.assertEqual(current[0].hour, 9)
            self.assertEqual(current[0].minute, 0)
            self.assertTrue(session.ledger.state()['sessions'])
            session.close()
            return {'status': 'OBSERVING'}
        with tempfile.TemporaryDirectory() as root:
            result = m.run_trading_day(root, transport=lambda *_: {'result': {'today': {
                'date': '2026-11-17', 'integrated': {'regularMarket': {
                    'startTime': '2026-11-17T09:00:00+09:00',
                    'endTime': '2026-11-17T15:30:00+09:00'}}}}},
                clock=lambda: current[0], sleep=sleep, run_session=run)
            self.assertEqual(result['status'], 'OBSERVING')

    def test_restart_preserves_cash_sequences_names_and_notification_metadata(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            s.set_meta('symbol_names', {'005930': '삼성전자'})
            s.set_meta('telegram_fill_deliveries_v1', {'fill-x': {'status': 'RESERVED'}})
            sequence = s.ledger.state()['last_sequence']
            s.close()
            s = m.open_or_resume_session(root, '2026-11-17')
            self.assertEqual(s.ledger.meta['symbol_names'], {'005930': '삼성전자'})
            self.assertIn('fill-x', s.ledger.meta['telegram_fill_deliveries_v1'])
            s.on_clock(at='2026-11-17T09:11:00+09:00')
            self.assertGreater(s.ledger.state()['last_sequence'], sequence)
            self.assertTrue(s.ledger.audit()['cash_identity'])
            s.close()

    def test_closed_previous_day_cash_carries_and_closed_today_does_not_reopen(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-16', initial_capital='9995188.8384')
            s.on_session(normal_session_verified=True, at='2026-11-16T09:10:00+09:00')
            s.close_session(at='2026-11-16T15:30:00+09:00')
            s.close()
            self.assertIsNone(m.open_or_resume_session(root, '2026-11-16'))
            s = m.open_or_resume_session(root, '2026-11-17')
            self.assertEqual(s.ledger.meta['initial_capital'], '9995188.8384')
            s.close()

    def test_actual_fills_notify_once_and_resume_restores_completed_candles(self):
        m = self.module()
        fixture = Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json'
        rows = json.loads(fixture.read_text(encoding='utf-8'))['events']
        # Derive each fixture's completed bars from actual WS-shaped trade
        # inputs; no REST candle transport exists in the daemon.
        from stock_assistant.daytrade.stream import LiveCandleBuilder
        for row in rows:
            if row['kind'] != 'candles':
                continue
            builder = LiveCandleBuilder()
            bars = row['payload']['result']['candles']
            beginning = datetime.fromisoformat(bars[0]['timestamp']) - timedelta(minutes=1)
            builder.set_active({row['symbol']}, at=beginning)
            for bar in bars:
                stamp = datetime.fromisoformat(bar['timestamp']) - timedelta(seconds=30)
                builder.update({'type': 'message', 'topic': 'trade:kr:'+row['symbol'], 'data': {
                    'timestamp': stamp.isoformat(), 'currency': 'KRW', 'price': bar['closePrice'],
                    'volume': bar['volume']}}, at=stamp)
                builder.advance(at=bar['timestamp'])
            row['payload']['result']['candles'] = builder.history(row['symbol'])
            row['source'] = 'TOSS_WS_COMPLETED_1M'
        sent = []
        def sender(message):
            sent.append(message)
            return {'status': 'SENT', 'http_status': 200}
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-13', send_fn=sender)
            for row in rows:
                if row['kind'] == 'end_session':
                    break
                s.add(row)
            fills = s.ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0]
            self.assertGreater(fills, 0)
            self.assertEqual(len(sent), fills)
            self.assertEqual({r[0] for r in s.ledger.connection.execute('SELECT side FROM paper_fills')}, {'BUY', 'SELL'})
            bars = {k: len(v.bars) for k,v in s._runner.strategy._states.items()}
            s.close()
            s = m.open_or_resume_session(root, '2026-11-13', send_fn=sender)
            self.assertEqual({k: len(v.bars) for k,v in s._runner.strategy._states.items()}, bars)
            m.notify_new_fills(s.ledger, sender)
            self.assertEqual(len(sent), fills)
            s.close()

    def test_restart_before_first_checkpoint_and_reboot_monotonic_offset(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            s.close()
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00', received_monotonic=1e9)
            s.close()
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_clock(at='2026-11-17T09:11:00+09:00', received_monotonic=1e9+1)
            self.assertTrue(s.ledger.audit()['cash_identity'])
            s.close()

    def test_restart_bars_can_feed_next_completed_live_bar(self):
        m = self.module()
        from stock_assistant.daytrade.stream import LiveCandleBuilder
        rows = json.loads((Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json').read_text(encoding='utf-8'))['events']
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-13')
            for row in rows[:4]: s.add(row)
            s.close()
            s = m.open_or_resume_session(root, '2026-11-13')
            builder = m.restore_candle_builder(s, at='2026-11-13T10:05:00+09:00')
            builder.update({'type': 'message', 'topic': 'trade:kr:005930', 'data': {
                'timestamp': '2026-11-13T10:05:30+09:00', 'currency': 'KRW', 'price': '11000', 'volume': '5'}},
                at='2026-11-13T10:05:30+09:00')
            bars = builder.advance(at='2026-11-13T10:06:01+09:00')['005930']
            s.add({'kind': 'candles', 'symbol': '005930', 'source': 'TOSS_WS_COMPLETED_1M',
                'payload': {'result': {'candles': bars}}, 'at': '2026-11-13T10:06:01+09:00', 'received_monotonic': 99})
            self.assertEqual(len(bars), 5)
            self.assertTrue(s.ledger.audit()['cash_identity'])
            s.close()

    def test_restart_keeps_more_than_twenty_bars_for_indicator_continuity(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_session(normal_session_verified=True, at='2026-11-17T09:00:00+09:00')
            bars = []
            for minute in range(1, 36):
                at = datetime(2026, 11, 17, 9, minute, tzinfo=KST)
                price = str(10000+minute)
                bars.append({'currency': 'KRW', 'timestamp': at.isoformat(), 'openPrice': price,
                    'highPrice': price, 'lowPrice': price, 'closePrice': price, 'volume': '1000'})
                s.add({'kind': 'candles', 'symbol': '005930', 'source': 'TOSS_WS_COMPLETED_1M',
                    'payload': {'result': {'candles': bars[-20:]}},
                    'at': (at+timedelta(seconds=1)).isoformat(), 'received_monotonic': minute})
            expected = dict(s._runner.strategy._states['005930'].bars)
            s.close()
            s = m.open_or_resume_session(root, '2026-11-17')
            try:
                self.assertEqual(s._runner.strategy._states['005930'].bars, expected)
            finally:
                s.close()

    def test_successful_rankings_recover_only_http_locks_after_restart(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            at = '2026-11-17T09:10:00+09:00'
            s.on_session(normal_session_verified=True, at=at)
            for lock, reason in [('DATA_DEGRADED', 'HTTP:RANKINGS'), ('RATE_LIMIT_DEGRADED', 'HTTP:RANKINGS'),
                                 ('DATA_DEGRADED', 'trade:005930'), ('LOSS_LOCKED', 'DAILY_NET_EQUITY_LOSS')]:
                s._runner.emit('LOCK', {'lock': lock, 'reason': reason}, at)
            s.close()
            s = m.open_or_resume_session(root, '2026-11-17')
            m.recover_rankings(s, at=at)
            reasons = s.ledger.state()['risk']['reasons']
            self.assertNotIn('RATE_LIMIT_DEGRADED', reasons)
            self.assertEqual(reasons['DATA_DEGRADED'], ['trade:005930'])
            self.assertIn('LOSS_LOCKED', reasons)
            s.close()

    def test_abnormal_session_skips_and_failed_calendar_is_not_holiday(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            response = {'result': {'today': {'date': '2026-11-17', 'integrated': {'regularMarket': {
                'startTime': '2026-11-17T10:00:00+09:00', 'endTime': '2026-11-17T16:30:00+09:00'}}}}}
            result = m.run_trading_day(root, transport=lambda *_: response,
                clock=lambda: datetime(2026, 11, 17, 9, tzinfo=KST))
            self.assertEqual(result['status'], 'MARKET_HOLIDAY_OR_NON_STANDARD_SESSION')
            self.assertFalse((Path(root)/'daily').exists())
            from stock_assistant.daytrade.http import HttpResult
            with self.assertRaisesRegex(RuntimeError, 'CALENDAR_HTTP_FAILED'):
                m.run_trading_day(root, transport=lambda *_: HttpResult(503, {}, {}))

    def test_after_hours_does_not_create_new_day(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            response = {'result': {'today': {'date': '2026-11-17', 'integrated': {'regularMarket': {
                'startTime': '2026-11-17T09:00:00+09:00', 'endTime': '2026-11-17T15:30:00+09:00'}}}}}
            result = m.run_trading_day(root, transport=lambda *_: response,
                clock=lambda: datetime(2026, 11, 17, 16, tzinfo=KST))
            self.assertEqual(result['status'], 'AFTER_MARKET_NO_COLLECTION')
            self.assertFalse((Path(root)/'daily').exists())

    def test_midday_sigterm_preserves_observing_and_closes_socket(self):
        m = self.module()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            m.finish_session(s, None, at='2026-11-17T10:00:00+09:00', end='2026-11-17T15:30:00+09:00')
            s = m.open_or_resume_session(root, '2026-11-17')
            self.assertIsNotNone(s)
            self.assertEqual(s.ledger.state()['sessions']['2026-11-17'], 'OBSERVING')
            s.close()

    def test_shutdown_unsubscribes_closes_and_writes_reports(self):
        m = self.module()
        class Socket:
            sent = []
            closed = False
            def send(self, text): self.sent.append(text)
            def close(self): self.closed = True
        class Adapter:
            socket = Socket()
            def close(self): self.socket.close()
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17')
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            adapter = Adapter()
            m.finish_session(s, adapter, at='2026-11-17T15:30:00+09:00', end='2026-11-17T15:30:00+09:00')
            self.assertIn('[]', adapter.socket.sent)
            self.assertTrue(adapter.socket.closed)
            report = json.loads((Path(root)/'daily/2026-11-17/report.json').read_text(encoding='utf-8'))
            self.assertTrue(report['audit']['cash_identity'])
            self.assertIsNone(m.open_or_resume_session(root, '2026-11-17'))

    def test_collector_drives_single_socket_completed_bars_names_and_market_close(self):
        m = self.module()
        from stock_assistant.daytrade.http import HttpResult
        import threading
        from copy import deepcopy
        rows = json.loads((Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json').read_text(encoding='utf-8'))['events']
        current = [datetime(2026, 11, 17, 15, 24, tzinfo=KST)]
        end = current[0].replace(minute=30)
        class Transport:
            statuses = {'200': 0}
            requests = []
            def __call__(self, path, query):
                self.statuses['200'] += 1
                self.requests.append({'path': path, 'http_status': 200})
                if path == '/api/v1/stocks':
                    return HttpResult(200, {'result': [{'symbol': '005930', 'name': '삼성전자'},
                        {'symbol': '000660', 'name': 'SK하이닉스'}]}, {})
                source = deepcopy(next(r['payload'] for r in rows if r['kind'] == 'rankings'))
                source['result']['rankedAt'] = current[0].isoformat()
                return HttpResult(200, source, {})
        class Socket:
            sent = []
            ack = None
            closed = False
            def send(self, raw):
                self.sent.append(raw)
                if raw in ('PING', '[]'): return
                declaration = json.loads(raw)
                self.ack = {'type': 'subscriptions', 'id': declaration[0]['id'], 'rejected': [],
                    'subscribed': [f"{r['type']}:{code}" for r in declaration[1:] for code in r['codes']]}
            def recv(self, **_kwargs):
                if self.ack is not None:
                    ack, self.ack = self.ack, None
                    return json.dumps(ack)
                current[0] += timedelta(seconds=1)
                return json.dumps({'type': 'message', 'topic': 'trade:kr:005930', 'data': {
                    'timestamp': current[0].isoformat(), 'currency': 'KRW', 'price': '10000', 'volume': '1000'}})
            def close(self): self.closed = True
        sock = Socket()
        with tempfile.TemporaryDirectory() as root:
            session = m.open_or_resume_session(root, '2026-11-17')
            session.on_session(normal_session_verified=True, at=current[0])
            collector = m.Collector(Transport(), lambda: 'fake', lambda _: None,
                threading.Event(), root, clock=lambda: current[0])
            with patch.object(m, 'runtime_socket_factory', return_value=lambda: sock):
                result = collector.run(session, end)
            self.assertEqual(result['status'], 'CLOSED')
            self.assertEqual(collector.stats['socket_connections'], 1)
            self.assertTrue(sock.closed)
            self.assertIn('[]', sock.sent)
            self.assertGreaterEqual(len(session._runner.strategy._states['005930'].bars), 4)
            self.assertEqual(session.ledger.meta['symbol_names']['005930'], '삼성전자')


if __name__ == '__main__': unittest.main()
