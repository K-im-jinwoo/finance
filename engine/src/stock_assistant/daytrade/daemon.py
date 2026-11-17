"""Daily, restartable paper service. No brokerage account/order endpoints exist here."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time as daytime
import json
import os
from pathlib import Path
import queue
import random
import signal
import threading
import time

from .bot import BotConfig, _PaperSession
from .contracts import KST, canonical, digest, instant, number
from .ledger import DaytradeLedger
from .market import MarketEvent
from .report import APPROVED_COST_STATUS, render_markdown_report
from .stream import (LiveSessionBridge, LivePaperSession, LiveCandleBuilder, LiveFrameBuffer,
                     _configuration)
from .telegram import notify_new_fills
from .websocket import WebSocketAdapter, runtime_socket_factory


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(canonical(value), encoding='utf-8')
    os.replace(temporary, path)


class DurablePaperSession(LivePaperSession):
    """Restore the runner without replaying a committed intent or fill."""
    def set_meta(self, key, value):
        with self.ledger.connection:
            self.ledger.connection.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))
        self.ledger.meta[key] = value

    def add(self, record):
        self.input_sequence += 1
        record = {'id': f'daemon-{self.input_sequence:012d}', 'sequence': self.input_sequence, **record}
        result = super().add(record)
        # Durable delivery reservations live independently of runner summaries.
        if self.send_fn is not None:
            try:
                results = notify_new_fills(self.ledger, self.send_fn)
                if results:
                    self.delivery_results.extend(results)
            except Exception:
                self.notification_errors += 1
        # Keep only the ordering watermark; the audited SQLite events are durable.
        if len(self._events) > 1000:
            self._events = self._events[-1:]
            self._seen = {self._events[0]['id']: digest(self._events[0])}
        return result


def _resume(root, send_fn):
    ledger = DaytradeLedger(root)
    try:
        if ledger.meta.get('cost_policy_status') != APPROVED_COST_STATUS or not ledger.meta.get('paper_ready'):
            raise ValueError('APPROVED_PAPER_LEDGER_REQUIRED')
        settings = ledger.meta.get('daemon_settings', {})
        s = DurablePaperSession.__new__(DurablePaperSession)
        LiveSessionBridge.__init__(s, **settings)
        _, quality, costs, scanner, strategy = _configuration(s.settings)
        config = BotConfig(ledger.meta['initial_capital'], ledger.meta['policy_version'],
            ledger.meta['replay_seed'], quality, costs, scanner, strategy)
        s.ledger, s.ws_adapter = ledger, None
        s._rankings, s._risk_metadata = {}, {}
        s._closed = s._session_ended = False
        s._source_hash = ledger.meta.get('_bot_summary', {}).get('dataset_hash', digest([]))
        s._runner = _PaperSession(ledger, config, s._source_hash, compact_live=True)
        s._runner.meta.update({key: ledger.meta[key] for key in
            ('paper_ready', 'cost_policy_status', 'stream_provenance')})
        summary = ledger.meta.get('_bot_summary', {})
        s._runner.meta.update(summary)
        s._runner.sequence = ledger.state()['last_sequence']
        s._runner.inactive = set(summary.get('inactive_intents', []))
        # Stale partial BUY remainders must never execute on a new connection.
        s._runner.inactive.update(k for k,v in ledger.state()['intents'].items() if v['side'] == 'BUY')
        history = summary.get('scan_history', [])
        if history:
            s._runner.watches = {r['symbol']: r for r in history[-1]['selected']}
        # Restore all rolling candle observations to preserve the strategy's
        # 200-bar indicator window; the newest snapshot alone contains only 20.
        # This feeds the pure strategy cache, never ledger intents or fills.
        latest = {}
        for raw, in ledger.connection.execute('SELECT o.payload FROM observations o JOIN events e ON e.id=o.event_id ORDER BY e.sequence'):
            value = json.loads(raw)
            if value['kind'] == 'candles':
                value['issues'] = tuple(value['issues'])
                s._runner.strategy.on_event(MarketEvent(**value))
            else:
                latest[(value['kind'], value['symbol'])] = value
        for value in sorted(latest.values(), key=lambda v: instant(v['received_at_utc'])):
            value['issues'] = tuple(value['issues'])
            event = MarketEvent(**value)
            if event.kind in {'candles', 'trade', 'orderbook'}:
                s._runner.strategy.on_event(event)
        s.input_sequence = max(ledger.state()['last_sequence'], summary.get('daemon_input_sequence', 0))
        s.send_fn, s.delivery_results, s.notification_errors = send_fn, [], 0
        return s
    except Exception:
        ledger.close()
        raise


def _closed(state):
    return bool(state['sessions']) and all(v in {'CLOSED', 'UNFLATTENED'} for v in state['sessions'].values())


def _prior_capital(state_root, day, default):
    daily = Path(state_root)/'daily'
    if not daily.exists():
        return default
    for path in sorted(daily.iterdir(), reverse=True):
        if not path.is_dir() or path.name >= day:
            continue
        try:
            date.fromisoformat(path.name)
        except ValueError:
            continue
        if not (path/'.runtime/intraday-paper.sqlite3').is_file():
            continue
        previous = DaytradeLedger(path)
        try:
            state = previous.state()
            if any(state['positions'].values()) or 'UNFLATTENED' in state['sessions'].values():
                raise ValueError('PRIOR_DAY_UNFLATTENED_REQUIRES_RECONCILIATION')
            if state['sessions'] and all(v == 'CLOSED' for v in state['sessions'].values()):
                return state['cash']
            raise ValueError('PRIOR_DAY_NOT_CLOSED_REQUIRES_RECONCILIATION')
        finally:
            previous.close()
    return default


def _update_latest(state_root, root):
    if os.name == 'nt':  # Windows test accounts may lack symlink privileges.
        atomic_json(Path(state_root)/'latest.json', {'path': str(root)})
        return
    temporary = Path(state_root)/'latest.new'
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(root.resolve(), target_is_directory=True)
    os.replace(temporary, Path(state_root)/'latest')


def open_or_resume_session(state_root, day, *, initial_capital='10000000', send_fn=None):
    date.fromisoformat(day)
    root = Path(state_root)/'daily'/day
    root.mkdir(parents=True, exist_ok=True)
    if (root/'.runtime/intraday-paper.sqlite3').exists():
        s = _resume(root, send_fn)
        _update_latest(state_root, root)
        if _closed(s.ledger.state()):
            s.close()
            return None
        return s
    capital = _prior_capital(state_root, day, initial_capital)
    s = DurablePaperSession(root, websocket_slot_available=True, initial_capital=capital,
        stream_kind='RECORDED', scanner_config={'max_watch': 8})
    s.input_sequence = 0
    s.send_fn, s.delivery_results, s.notification_errors = send_fn, [], 0
    s.set_meta('daemon_settings', s.settings)
    _update_latest(state_root, root)
    return s


def finish_session(session, adapter, *, at, end):
    try:
        session.on_clock(at=at, received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
        if instant(at) >= instant(end):
            session.close_session(at=at, received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
        session._runner.checkpoint(at, latest_only=True)
        report = session.report()
        root = session.ledger.path.parent.parent
        atomic_json(root/'report.json', report)
        (root/'report.md').write_text(render_markdown_report(report), encoding='utf-8')
    finally:
        try:
            if adapter is not None:
                try:
                    if adapter.socket is not None:
                        adapter.socket.send('[]')
                finally:
                    adapter.close()
        finally:
            session.close()


def run_trading_day(state_root, *, transport, clock=lambda: datetime.now(KST),
                    sleep=time.sleep, run_session=None, send_fn=None, stop=None):
    now = instant(clock()).astimezone(KST)
    day = now.date().isoformat()
    response = transport('/api/v1/market-calendar/KR', {'date': day})
    if hasattr(response, 'status'):
        if response.status != 200:
            raise RuntimeError('CALENDAR_HTTP_FAILED')
        response = response.payload
    today = response['result']['today']
    regular = (today.get('integrated') or {}).get('regularMarket')
    if (today.get('date') != day or not regular or
            instant(regular['startTime']).astimezone(KST) != datetime.combine(now.date(), daytime(9), KST) or
            instant(regular['endTime']).astimezone(KST) != datetime.combine(now.date(), daytime(15, 30), KST)):
        return {'status': 'MARKET_HOLIDAY_OR_NON_STANDARD_SESSION'}
    start, end = instant(regular['startTime']), instant(regular['endTime'])
    while instant(clock()) < start:
        if stop is not None and stop.is_set():
            return {'status': 'STOPPED_BEFORE_OPEN'}
        sleep(min(1, (start - instant(clock())).total_seconds()))
    # A late service activation does not fabricate an entire missed trading day.
    root = Path(state_root)/'daily'/day
    if instant(clock()) >= end and not (root/'.runtime/intraday-paper.sqlite3').exists():
        return {'status': 'AFTER_MARKET_NO_COLLECTION'}
    session = open_or_resume_session(state_root, day, send_fn=send_fn)
    if session is None:
        return {'status': 'ALREADY_CLOSED'}
    if instant(clock()) >= end:
        finish_session(session, None, at=clock(), end=end)
        return {'status': 'CLOSED_WITHOUT_COLLECTION'}
    session.on_session(normal_session_verified=True, at=clock(),
        received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
    try:
        if run_session is None:
            raise ValueError('EXPLICIT_SESSION_DRIVER_REQUIRED')
        return run_session(session, end)
    except BaseException:
        if not session._closed:
            finish_session(session, None, at=clock(), end=end)
        raise


class StickyAdapter(WebSocketAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sticky, self.held = [], []

    def replace(self, topics, *, now):
        codes = [t.rsplit(':', 1)[1] for t in sorted(topics)]
        names = list(dict.fromkeys(self.held + self.sticky + codes))[:8]
        self.sticky = names
        desired = {f'{kind}:kr:{code}' for kind in ('trade', 'orderbook') for code in names}
        if desired == self.desired or self.pending is not None:
            return
        super().replace(desired, now=now)


def restore_candle_builder(session, *, at):
    builder = LiveCandleBuilder(grace_seconds=1)
    for code, state in session._runner.strategy._states.items():
        if not state.bars:
            continue
        builder.set_active({code} | set(builder.states), at=at)
        # Strategy stores normalized bars; the builder's output must stay in
        # provider format so normalize() can validate the complete history again.
        bars = [{'timestamp': bar['timestamp'], 'currency': 'KRW', 'volume': bar['volume'],
            **{raw: bar[key] for key,raw in (('open','openPrice'), ('high','highPrice'),
                ('low','lowPrice'), ('close','closePrice'))}} for bar in list(state.bars.values())[-20:]]
        builder.states[code]['bars'] = bars
        builder.states[code]['last_close'] = number(bars[-1]['closePrice'], positive=True)
    return builder


def recover_rankings(session, *, at):
    reasons = session.ledger.state()['risk']['reasons']
    for lock in ('DATA_DEGRADED', 'RATE_LIMIT_DEGRADED'):
        if 'HTTP:RANKINGS' in reasons.get(lock, []):
            session._runner.emit('RECOVER', {'lock': lock, 'reason': 'HTTP:RANKINGS', 'verified': True}, at)
    session._runner.checkpoint(at, latest_only=True)


class TrackedSocket:
    def __init__(self, raw, stats):
        self.raw, self.stats, self.closed = raw, stats, False

    def send(self, text):
        return self.raw.send(text)

    def recv(self, **kwargs):
        return self.raw.recv(**kwargs)

    def close(self):
        if self.closed:
            return
        try:
            try:
                self.raw.send('[]')
            except Exception:
                pass
            self.raw.close()
        finally:
            self.closed = True
            self.stats['active_sockets'] -= 1


class Collector:
    def __init__(self, transport, token, send_fn, stop, state_root, *, clock=lambda: datetime.now(KST)):
        self.transport, self.token, self.send_fn, self.stop = transport, token, send_fn, stop
        self.state_root, self.clock = Path(state_root), clock
        self.jobs, self.results = queue.Queue(maxsize=8), queue.Queue()
        self.stats = {'orders_enabled': False, 'socket_connections': 0, 'peak_active_sockets': 0, 'active_sockets': 0,
                      'ws_frames': 0, 'notification_errors': 0}

    def run(self, session, end):
        from .http import RestAdapter
        rest = RestAdapter(self.transport, rng=random.Random(42))
        builder = restore_candle_builder(session, at=self.clock())
        buffer = LiveFrameBuffer(builder, trade_interval=1, book_interval=.5, max_age_seconds=3.5)
        offset = max(0, session.ledger.state()['last_monotonic'] - time.monotonic()) + .001
        mono = lambda: time.monotonic() + offset
        def frame(value):
            buffer.push(value, at=self.clock())
            self.stats['ws_frames'] += 1
        def connect():
            if self.stats['active_sockets']:
                raise RuntimeError('SOCKET_OVERLAP_REFUSED')
            raw = runtime_socket_factory(self.token, slot_confirmed=True)()
            self.stats['socket_connections'] += 1
            self.stats['active_sockets'] += 1
            self.stats['peak_active_sockets'] = max(self.stats['peak_active_sockets'], self.stats['active_sockets'])
            return TrackedSocket(raw, self.stats)
        adapter = StickyAdapter(connect, rng=random.Random(42), on_message=frame)
        # Start from held names first; fresh rankings add a bounded sticky pool.
        adapter.held = list(session.ledger.state()['positions'])
        session.ws_adapter = adapter
        def worker():
            while not self.stop.is_set():
                try:
                    kind, query = self.jobs.get(timeout=.25)
                except queue.Empty:
                    continue
                try:
                    value = rest.fetch('/api/v1/rankings', query, now=mono(), purpose='EXIT_MONITOR') if kind != 'names' else self.transport('/api/v1/stocks', query)
                    if kind != 'names' and value['status'] == 'OK':
                        rest.recover('RANKINGS', verified=True)
                        value['entry_blocked'] = rest.entry_blocked
                    self.results.put((kind, value))
                except Exception:
                    self.results.put((kind, None))
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        next_rank, last_clock, names_pending = 0, 0, False
        queued = set()
        session.ledger.connection.execute('PRAGMA journal_mode=WAL')
        session.ledger.connection.execute('PRAGMA synchronous=NORMAL')
        try:
            while not self.stop.is_set() and instant(self.clock()) < end:
                current = mono()
                if current >= next_rank:
                    for kind in ('MARKET_TRADING_AMOUNT', 'MARKET_TRADING_VOLUME'):
                        if kind not in queued:
                            self.jobs.put((kind, {'marketCountry': 'KR', 'duration': 'realtime', 'count': 100,
                                'excludeInvestmentCaution': 'true', 'type': kind}))
                            queued.add(kind)
                    next_rank = current + 18
                if adapter.pending is None:
                    while not self.results.empty():
                        kind, value = self.results.get()
                        if kind == 'names':
                            names_pending = False
                            if value is not None and value.status == 200:
                                names = session.ledger.meta.get('symbol_names', {}).copy()
                                result = value.payload['result']
                                rows = result.get('stocks', []) if isinstance(result, dict) else result
                                for row in rows:
                                    code, name = row.get('symbol'), row.get('name')
                                    if isinstance(code, str) and isinstance(name, str):
                                        names[code] = name
                                session.set_meta('symbol_names', names)
                            continue
                        queued.discard(kind)
                        if value is None:
                            raise RuntimeError('REST_WORKER_FAILED')
                        adapter.held = list(session.ledger.state()['positions'])
                        session.on_rest('/api/v1/rankings', value, at=self.clock(), received_monotonic=mono(), ranking_type=kind)
                        if value.get('status') == 'OK':
                            recover_rankings(session, at=self.clock())
                        elif value.get('http_status') in {401, 403}:
                            raise RuntimeError('SHARED_AUTH_REJECTED_NO_MUTATION')
                        if adapter.pending is not None:
                            break
                if adapter.socket is None and adapter.desired and adapter.retry_at is None:
                    if adapter.status == 'AUTH_REVOKED':
                        raise RuntimeError('SHARED_AUTH_REVOKED_NO_MUTATION')
                    adapter.connect(now=mono())
                adapter.tick(now=mono())
                if adapter.socket is not None:
                    try:
                        raw = adapter.socket.recv(timeout=.02)
                    except TimeoutError:
                        raw = None
                    except (OSError, ConnectionError):
                        adapter._schedule(mono())
                        raw = None
                    if raw is not None:
                        adapter.on_frame(raw, now=mono())
                        adapter.messages.clear()
                active = {t.rsplit(':', 1)[1] for t in adapter.active}
                builder.set_active(active, at=self.clock())
                if not names_pending:
                    missing = sorted(active - set(session.ledger.meta.get('symbol_names', {})))
                    if missing:
                        self.jobs.put(('names', {'symbols': ','.join(missing)}))
                        names_pending = True
                for value in buffer.drain(at=self.clock(), monotonic=mono()):
                    session.on_frame(value, at=self.clock(), received_monotonic=mono())
                for code, bars in builder.advance(at=self.clock()).items():
                    session.add({'kind': 'candles', 'symbol': code, 'source': 'TOSS_WS_COMPLETED_1M',
                        'payload': {'result': {'candles': bars}}, 'at': self.clock().isoformat(), 'received_monotonic': mono()})
                if current - last_clock >= 1:
                    # Validate cached-token TTL even while a socket stays connected.
                    self.token()
                    session.on_clock(at=self.clock(), received_monotonic=mono())
                    self.stats.update(status='OBSERVING', at=self.clock().isoformat(),
                        ws=adapter.snapshot(), candles=builder.snapshot(), buffer=dict(buffer.counts),
                        http_status=dict(self.transport.statuses), rest_requests=list(self.transport.requests),
                        selected=list(session._runner.watches), initial_capital=session.ledger.meta['initial_capital'],
                        symbol_names=session.ledger.meta.get('symbol_names', {}),
                        notification_errors=session.notification_errors, deliveries=session.delivery_results[-30:])
                    atomic_json(self.state_root/'status.json', self.stats)
                    last_clock = current
                self.stop.wait(.005)
            return {'status': 'STOPPED' if self.stop.is_set() else 'CLOSED'}
        finally:
            self.stop.set()
            worker_thread.join(timeout=12)
            finish_session(session, adapter, at=self.clock(), end=end)


def main():
    from .service_runtime import ReadOnlyTransport, read_token, telegram_sender
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', required=True)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # flock is process-scoped; a second manually started instance never shares a ledger/socket.
    import fcntl
    root = Path(args.state_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root/'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        transport = ReadOnlyTransport(read_token, stop)
        sender = telegram_sender()
        collector = Collector(transport, read_token, sender, stop, root)
        try:
            result = run_trading_day(root, transport=transport, send_fn=sender, stop=stop,
                sleep=lambda s: stop.wait(s), run_session=collector.run)
            atomic_json(root/'exit.json', result)
            print(canonical(result), flush=True)
        except Exception as error:
            # No raw exception text: upstream URLs may contain credentials.
            import traceback
            atomic_json(root/'exit.json', {'status': 'FAILED', 'error_class': type(error).__name__,
                'frames': [{'file': Path(f.filename).name, 'line': f.lineno} for f in traceback.extract_tb(error.__traceback__)]})
            print(canonical({'status': 'FAILED', 'error_class': type(error).__name__}), flush=True)
            raise SystemExit(1) from None


if __name__ == '__main__':
    main()
