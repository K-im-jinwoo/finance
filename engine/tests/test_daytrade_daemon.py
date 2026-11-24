"""Offline service lifecycle and real ledger restart tests; no external sending."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
import importlib
from io import StringIO
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import tempfile
import threading
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


class DailySummaryDaemonTests(unittest.TestCase):
    def test_market_close_writes_reports_then_sends_once_and_closed_restart_sends_nothing(self):
        m = importlib.import_module('stock_assistant.daytrade.daemon')
        with tempfile.TemporaryDirectory() as root:
            calls = []
            def send(text):
                path = Path(root)/'daily/2026-11-17'
                self.assertTrue((path/'report.md').is_file())
                report = json.loads((path/'report.json').read_text(encoding='utf-8'))
                self.assertEqual(report['sessions'], {'2026-11-17':'CLOSED'})
                calls.append(text)
                return {'status':'SENT','http_status':200}
            s = m.open_or_resume_session(root, '2026-11-17', send_fn=send)
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            m.finish_session(s, None, at='2026-11-17T15:30:00+09:00', end='2026-11-17T15:30:00+09:00')
            self.assertEqual(len(calls), 1)
            self.assertEqual(s.delivery_results, [{'session_date':'2026-11-17','status':'SENT','http_status':200}])
            self.assertIsNone(m.open_or_resume_session(root, '2026-11-17', send_fn=send))
            self.assertEqual(len(calls), 1)

    def test_intraday_shutdown_never_claims_or_sends_daily_summary(self):
        m = importlib.import_module('stock_assistant.daytrade.daemon')
        with tempfile.TemporaryDirectory() as root:
            s = m.open_or_resume_session(root, '2026-11-17', send_fn=lambda _: self.fail('intraday summary sent'))
            s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            m.finish_session(s, None, at='2026-11-17T15:29:59+09:00', end='2026-11-17T15:30:00+09:00')
            s = m.open_or_resume_session(root, '2026-11-17')
            try:
                self.assertNotIn('telegram_daily_summary_v1', s.ledger.meta)
                self.assertEqual(s.ledger.state()['sessions']['2026-11-17'], 'OBSERVING')
            finally:
                s.close()

    def test_notification_failure_cannot_prevent_adapter_and_ledger_close(self):
        m = importlib.import_module('stock_assistant.daytrade.daemon')
        class Adapter:
            socket = None
            closed = False
            def close(self): self.closed = True
        for injected_error in (False, True):
            with self.subTest(injected_error=injected_error), tempfile.TemporaryDirectory() as root:
                def send(_): raise TimeoutError('sensitive-transport-secret')
                s = m.open_or_resume_session(root, '2026-11-17', send_fn=send)
                s.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
                adapter = Adapter()
                # A storage/notifier error and a caught transport failure both
                # leave the shutdown path operational and do not leak messages.
                from contextlib import nullcontext
                injection = patch.object(m, 'notify_daily_summary', side_effect=RuntimeError('sensitive-storage-secret'), create=True) if injected_error else nullcontext()
                with injection:
                    m.finish_session(s, adapter, at='2026-11-17T15:31:00+09:00', end='2026-11-17T15:30:00+09:00')
                self.assertTrue(adapter.closed)
                self.assertEqual(s.notification_errors, 1)
                self.assertNotIn('sensitive-', json.dumps(s.delivery_results))
                from stock_assistant.daytrade.ledger import DaytradeLedger
                other = DaytradeLedger(Path(root)/'daily/2026-11-17')
                try:
                    self.assertEqual(other.state()['sessions'], {'2026-11-17':'CLOSED'})
                    self.assertTrue(other.audit()['cash_identity'])
                    self.assertNotIn('sensitive-', json.dumps(list(other.connection.execute('SELECT * FROM meta'))))
                finally:
                    other.close()


class DaemonRestartGuardTests(unittest.TestCase):
    def setUp(self):
        self.daemon = importlib.import_module('stock_assistant.daytrade.daemon')
        self.runtime = importlib.import_module('stock_assistant.daytrade.service_runtime')
        self.now = datetime(2026, 11, 24, 9, 30, tzinfo=KST)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = StringIO()
        self.sent = []

    def send(self, text):
        self.assertEqual(self.failures()[-1]['notification_status'], 'RESERVED')
        self.sent.append(text)
        return {'status': 'SENT', 'http_status': 200}

    def run_service(self, result=None, *, error=None, create_sender=None):
        with (patch.object(self.runtime, 'telegram_sender', side_effect=create_sender,
                           return_value=self.send),
              patch.object(self.daemon, 'run_trading_day', side_effect=error,
                           return_value=result) as run,
              redirect_stdout(self.output)):
            code = self.daemon.run_service(self.root, stop=threading.Event(), clock=lambda: self.now)
        return code, run.call_count

    def failures(self):
        with closing(sqlite3.connect(self.root/'daemon-state.sqlite3')) as db:
            return [json.loads(raw) for raw, in db.execute('SELECT payload FROM daemon_failures ORDER BY id')]

    def test_error_records_only_safe_fields_and_stack_locations(self):
        closed = type('ConnectionClosedError', (Exception,), {'__module__': 'websockets.exceptions'})

        def fail(*args, **kwargs):
            raise closed('Bearer private-token, account=12345678901, https://broker.test/?key=private-query')

        self.assertEqual(self.run_service(error=fail), (1, 1))
        record, = self.failures()
        self.assertEqual(record['error_code'], 'NETWORK')
        self.assertEqual(record['error_class'], 'ConnectionClosedError')
        self.assertIn(len(record['frames']), range(1, 4))
        self.assertTrue(all(set(frame) == {'file', 'line'} for frame in record['frames']))
        exit_report = (self.root/'exit.json').read_text(encoding='utf-8')
        self.assertEqual(json.loads(exit_report), record)
        for secret in ('private-token', '12345678901', 'broker.test', 'private-query'):
            self.assertNotIn(secret, self.output.getvalue() + exit_report + json.dumps(record))

    def test_dns_and_tls_errors_are_network_failures(self):
        for error in (socket.gaierror('dns detail'), ssl.SSLError('tls detail')):
            with self.subTest(error_type=type(error)):
                self.assertEqual(self.run_service(error=error), (1, 1))
                self.assertEqual(self.failures()[-1]['error_code'], 'NETWORK')

    def test_interrupted_processes_count_once_and_stop_before_startup(self):
        for _ in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.run_service(error=KeyboardInterrupt())
        self.assertEqual(self.run_service(error=KeyboardInterrupt()), (0, 0))
        self.assertEqual(self.run_service(), (0, 0))
        records = self.failures()
        self.assertEqual([r['error_class'] for r in records], ['UnexpectedTermination'] * 3)
        self.assertEqual(records[-1]['entry_blocked'], True)
        self.assertEqual(len(self.sent), 1)

    def test_same_error_locks_existing_ledger_and_sends_once_across_restarts(self):
        day = self.now.date().isoformat()
        with self.daemon.open_or_resume_session(self.root, day) as session:
            session.on_session(normal_session_verified=True, at=self.now)
            intent, _ = session._runner.emit('INTENT', {'symbol': '005930', 'side': 'BUY', 'quantity': 1}, self.now)
            self.now += timedelta(seconds=1)
            quote, _ = session._runner.emit('MARKET', {'symbol': '005930', 'feed': 'orderbook',
                'received_monotonic': 1, 'payload': {'currency': 'KRW', 'timestamp': self.now.isoformat(),
                    'asks': [{'price': '10000', 'volume': '10'}], 'bids': [{'price': '9990', 'volume': '10'}]}}, self.now)
            session._runner.emit('FILL', {'intent_id': intent, 'quote_event_id': quote}, self.now)
            ledger_path = session.ledger.path.parent.parent

        error = RuntimeError('SHARED_TOKEN_EXPIRING_NO_RENEWAL')
        self.assertEqual(self.run_service(error=error), (1, 1))
        self.assertEqual(self.run_service(error=error), (1, 1))
        self.assertEqual(self.run_service(error=error), (0, 1))
        self.assertEqual(self.run_service(error=error), (0, 0))
        self.assertEqual(len(self.sent), 1)
        self.assertIn('AUTH', self.sent[0])
        self.assertIn('미청산 보유: 1종목 · 손절·시간청산 감시 중단', self.sent[0])
        record = self.failures()[-1]
        self.assertEqual((record['status'], record['notification_status']), ('RESTART_LOCKED', 'SENT'))
        with closing(self.daemon.DaytradeLedger(ledger_path)) as ledger:
            self.assertEqual(ledger.state()['positions'], {'005930': 1})
            self.assertEqual(ledger.state()['risk']['reasons']['DATA_DEGRADED'], ['DAEMON_RESTART_LIMIT'])
            self.assertEqual(ledger.state()['sessions'], {day: 'OBSERVING'})
            self.assertTrue(ledger.audit()['cash_identity'])

    def test_mixed_failure_limit_ignores_old_failures_and_clean_shutdown(self):
        def network_error():
            return TimeoutError('network')

        self.assertEqual(self.run_service(error=network_error()), (1, 1))
        self.now += timedelta(minutes=10, seconds=1)
        self.assertEqual(self.run_service({'status': 'STOPPED'}), (0, 1))
        errors = [network_error(), ValueError('data'), RuntimeError('unknown'),
                  sqlite3.DatabaseError('state'), network_error(), ValueError('data')]
        for error in errors[:-1]:
            self.assertEqual(self.run_service(error=error), (1, 1))
        self.assertEqual(self.run_service(error=errors[-1]), (0, 1))
        self.assertEqual(self.failures()[-1]['window_failures'], 6)
        self.assertEqual(len(self.sent), 1)

    def test_failed_alert_remains_single_attempt_and_setup_errors_are_classified(self):
        def fail_send(_):
            self.sent.append('attempt')
            raise TimeoutError('sensitive-notification')

        for expected in (1, 1, 0, 0):
            self.assertEqual(self.run_service(error=RuntimeError('fatal'), create_sender=lambda: fail_send)[0], expected)
        self.assertEqual(self.sent, ['attempt'])
        self.assertEqual(self.failures()[-1]['notification_status'], 'FAILED')
        self.assertNotIn('sensitive-notification', self.output.getvalue())

        self.now += timedelta(days=1)
        def bad_config():
            raise ValueError('EXISTING_BOT_TOKEN_REQUIRED')

        self.assertEqual(self.run_service(create_sender=bad_config), (1, 0))
        self.assertEqual(self.failures()[-1]['error_code'], 'AUTH')


if __name__ == '__main__': unittest.main()
