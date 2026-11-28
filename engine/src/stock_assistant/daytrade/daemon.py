"""Daily, restartable paper service. No brokerage account/order endpoints exist here."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, time as daytime, timedelta
import json
import os
from pathlib import Path
import queue
import random
import signal
import socket
import sqlite3
import ssl
import threading
import time
from traceback import walk_tb
from urllib.error import HTTPError, URLError

from ..telegram_routing import BotChannel
from .bot import BotConfig, _PaperSession
from .contracts import KST, canonical, digest, instant, number
from .ledger import DaytradeLedger
from .market import MarketEvent
from .report import APPROVED_COST_STATUS, render_markdown_report
from .stream import (LiveSessionBridge, LivePaperSession, LiveCandleBuilder, LiveFrameBuffer,
                     _configuration)
from .telegram import DELIVERY_POLICY, notify_daily_summary, notify_new_fills
from .websocket import WebSocketAdapter, runtime_socket_factory
from .live_config import merge_live_settings, live_settings as approved_live_settings
from .metadata import StockMetadataCache


RESTART_WINDOW_SECONDS = 10 * 60
MAX_FAILURES_PER_WINDOW = 5
SAME_ERROR_LIMIT = 3
RESTART_LOCK_REASON = 'DAEMON_RESTART_LIMIT'
AUTH_ERRORS = frozenset({
    'SHARED_TOKEN_EXPIRING_NO_RENEWAL', 'SHARED_AUTH_REJECTED_NO_MUTATION',
    'SHARED_AUTH_REVOKED_NO_MUTATION', 'AUTH_CACHE_SYMLINK_REFUSED',
    'EXISTING_BOT_TOKEN_REQUIRED', 'EXISTING_HOME_CHANNEL_REQUIRED',
    'TELEGRAM_CHAT_ID_INVALID', 'TELEGRAM_MESSAGE_THREAD_ID_INVALID',
    'APPROVED_TARGET_AMBIGUOUS', 'DAYTRADE_CLIENT_ID_FILE',
    'DAYTRADE_TOKEN_CACHE', 'DAYTRADE_TELEGRAM_PROFILE',
})
STATE_ERRORS = frozenset({
    'SOCKET_OVERLAP_REFUSED', 'APPROVED_PAPER_LEDGER_REQUIRED',
    'PRIOR_DAY_UNFLATTENED_REQUIRES_RECONCILIATION',
    'PRIOR_DAY_NOT_CLOSED_REQUIRES_RECONCILIATION', 'EXPLICIT_SESSION_DRIVER_REQUIRED',
})
WS_CLOSE_ERRORS = frozenset({'ConnectionClosed', 'ConnectionClosedOK', 'ConnectionClosedError'})
NETWORK_ERRORS = (ConnectionError, TimeoutError, URLError, socket.gaierror, ssl.SSLError)
STACK_FILES = frozenset({
    'daemon.py', 'websocket.py', 'service_runtime.py', 'stream.py', 'ledger.py',
    'http.py', 'bot.py', 'contracts.py', 'market.py', 'metadata.py', 'report.py',
    'risk.py', 'scanner.py', 'strategy.py', 'telegram.py', 'live_config.py',
})


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.new')
    temporary.write_text(canonical(value), encoding='utf-8')
    os.replace(temporary, path)


def _error_details(error):
    # Match known internal codes without serializing arbitrary exception strings or locals.
    detail = error.args[0] if error.args and type(error.args[0]) is str else None
    ws_close = next((cls.__name__ for cls in type(error).__mro__
                     if cls.__module__ == 'websockets.exceptions' and cls.__name__ in WS_CLOSE_ERRORS), None)
    if detail in AUTH_ERRORS or (isinstance(error, HTTPError) and error.code in {401, 403}):
        category = 'AUTH'
    elif ws_close or detail == 'CALENDAR_HTTP_FAILED' or isinstance(error, NETWORK_ERRORS):
        category = 'NETWORK'
    elif detail in STATE_ERRORS or isinstance(error, (sqlite3.Error, OSError)):
        category = 'STATE'
    elif isinstance(error, (KeyError, ValueError, TypeError)):
        category = 'DATA'
    else:
        category = 'UNKNOWN'
    safe_types = (RuntimeError, ConnectionError, ConnectionResetError, BrokenPipeError, TimeoutError,
                  HTTPError, URLError, socket.gaierror, ssl.SSLError, sqlite3.Error, sqlite3.DatabaseError,
                  sqlite3.OperationalError, OSError, PermissionError, json.JSONDecodeError,
                  ValueError, KeyError, TypeError, SystemExit)
    error_class = ws_close or next((cls.__name__ for cls in safe_types if type(error) is cls), 'Exception')
    frames = []
    external = None
    for frame, line in walk_tb(error.__traceback__):
        filename = Path(frame.f_code.co_filename).name
        module = frame.f_globals.get('__name__', '')
        if frame.f_code.co_filename == __file__ or (
                module.startswith('stock_assistant.daytrade.') and filename in STACK_FILES):
            frames.append({'file': filename, 'line': line})
        else:
            external = {'file': '[external]', 'line': line}
    if not frames and external is not None:
        frames.append(external)
    return {'error_code': category, 'error_class': error_class, 'frames': frames[-3:]}


class DaemonRestartGuard:
    """Keep the failure window and stop decision across systemd process restarts."""

    def __init__(self, db):
        self.db = db
        db.executescript('''
            CREATE TABLE IF NOT EXISTS daemon_failures (
                id INTEGER PRIMARY KEY, occurred_at INTEGER NOT NULL,
                error_code TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daemon_state (
                id INTEGER PRIMARY KEY CHECK (id = 1), running INTEGER NOT NULL,
                locked_day TEXT, locked_failure_id INTEGER REFERENCES daemon_failures(id)
            );
            INSERT OR IGNORE INTO daemon_state (id, running) VALUES (1, 0);
        ''')

    def start(self, at):
        running, day, failure_id = self.db.execute(
            'SELECT running, locked_day, locked_failure_id FROM daemon_state WHERE id=1').fetchone()
        if day == at.date().isoformat():
            raw, = self.db.execute('SELECT payload FROM daemon_failures WHERE id=?', (failure_id,)).fetchone()
            return None, json.loads(raw)
        interrupted = None
        if running:
            interrupted = self.fail(at, {'error_code': 'STATE', 'error_class': 'UnexpectedTermination', 'frames': []})
        if interrupted is None or not interrupted[1]['entry_blocked']:
            with self.db:
                self.db.execute('UPDATE daemon_state SET running=1 WHERE id=1')
        return interrupted

    def finish(self):
        with self.db:
            self.db.execute('UPDATE daemon_state SET running=0 WHERE id=1')

    def fail(self, at, details):
        stamp = int(at.timestamp())
        with self.db:
            count, same = self.db.execute('''
                SELECT count(*), count(CASE WHEN error_code=? THEN 1 END)
                FROM daemon_failures WHERE occurred_at BETWEEN ? AND ?
            ''', (details['error_code'], stamp - RESTART_WINDOW_SECONDS, stamp)).fetchone()
            count, same = count + 1, same + 1
            locked = count > MAX_FAILURES_PER_WINDOW or same >= SAME_ERROR_LIMIT
            record = {'status': 'RESTART_LOCKED' if locked else 'FAILED', 'at': at.isoformat(),
                      'entry_blocked': locked, 'orders_enabled': False, **details,
                      'window_failures': count, 'same_error_failures': same}
            if locked:
                # Reserve the single alert in the same transaction as the restart lock.
                record.update(notification_status='RESERVED', bot_channel=BotChannel.DAYTRADE.value,
                              delivery_policy=DELIVERY_POLICY)
            failure_id = self.db.execute('INSERT INTO daemon_failures (occurred_at, error_code, payload) VALUES (?,?,?)',
                                         (stamp, details['error_code'], canonical(record))).lastrowid
            if locked:
                self.db.execute('UPDATE daemon_state SET locked_day=?, locked_failure_id=? WHERE id=1',
                                (at.date().isoformat(), failure_id))
            self.db.execute('UPDATE daemon_state SET running=0 WHERE id=1')
        return failure_id, record

    def save(self, failure_id, record):
        with self.db:
            self.db.execute('UPDATE daemon_failures SET payload=? WHERE id=?', (canonical(record), failure_id))


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


def _resume(root, send_fn, live_settings=None):
    ledger = DaytradeLedger(root)
    try:
        if ledger.meta.get('cost_policy_status') != APPROVED_COST_STATUS or not ledger.meta.get('paper_ready'):
            raise ValueError('APPROVED_PAPER_LEDGER_REQUIRED')
        settings = merge_live_settings(ledger.meta.get('daemon_settings', {}),live_settings)
        s = DurablePaperSession.__new__(DurablePaperSession)
        LiveSessionBridge.__init__(s, **settings)
        _, quality, costs, scanner, strategy = _configuration(s.settings)
        config = BotConfig(ledger.meta['initial_capital'], ledger.meta['policy_version'],
            ledger.meta['replay_seed'], quality, costs, scanner, strategy,
            settings.get('max_daily_entries'),settings.get('reentry_cooldown_seconds'))
        s.ledger, s.ws_adapter = ledger, None
        s._rankings, s._risk_metadata = {}, {}
        s._closed = s._session_ended = False
        s._source_hash = ledger.meta.get('_bot_summary', {}).get('dataset_hash', digest([]))
        s._runner = _PaperSession(ledger, config, s._source_hash, compact_live=True)
        s._runner.meta.update({key: ledger.meta[key] for key in
            ('paper_ready', 'cost_policy_status', 'stream_provenance')})
        summary = ledger.meta.get('_bot_summary', {})
        s._runner.meta.update(summary)
        s._runner.trim_live_history()
        state = ledger.state()
        s._runner.sequence = state['last_sequence']
        s._runner.inactive = set(summary.get('inactive_intents', []))
        for keys, in ledger.connection.execute("""
            SELECT json_extract(payload, '$.data.superseded_partial_intents')
            FROM events WHERE sequence > ? AND kind='INTENT'
                AND json_type(payload, '$.data.superseded_partial_intents')='array'
        """, (summary.get('_checkpoint_sequence', 0),)):
            s._runner.inactive.update(json.loads(keys))
        # Stale partial BUY remainders must never execute on a new connection.
        s._runner.inactive.update(k for k,v in state['intents'].items() if v['side'] == 'BUY')
        history = s._runner.meta.get('scan_history', [])
        if history:
            s._runner.watches = {r['symbol']: r for r in history[-1]['selected']}
        if scanner.require_common_stock:
            # Yesterday's/old-policy watch set is not fresh reference validation.
            s._runner.watches={}
        # Candles rebuild the rolling bar window; held orderbooks rebuild the bid peaks.
        # Replay those histories and the remaining latest snapshots in one sequence.
        held = [code for code, quantity in state['positions'].items() if quantity > 0]
        for raw, in ledger.connection.execute("""
            WITH history AS (
                SELECT e.sequence, o.payload, json_extract(o.payload, '$.kind') AS kind,
                       json_extract(o.payload, '$.symbol') AS symbol
                FROM observations o JOIN events e ON e.id=o.event_id
            ),
            latest AS (
                SELECT max(sequence) FROM history WHERE kind IN ('trade', 'orderbook')
                GROUP BY kind, symbol
            )
            SELECT payload FROM history
            WHERE kind='candles'
                OR (kind='orderbook' AND symbol IN (SELECT value FROM json_each(?)))
                OR sequence IN (SELECT * FROM latest)
            ORDER BY sequence
        """, (json.dumps(held),)):
            value = json.loads(raw)
            value['issues'] = tuple(value['issues'])
            s._runner.strategy.on_event(MarketEvent(**value))
        s.input_sequence = max(state['last_sequence'], summary.get('daemon_input_sequence', 0))
        s.send_fn, s.delivery_results, s.notification_errors = send_fn, [], 0
        s._checkpoint_interval=4 if scanner.require_common_stock else 0
        s._checkpoint_clock=time.monotonic
        s._last_checkpoint=float('-inf')
        s._checkpoint_fills=ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0]
        s._checkpoint_critical=0
        s.set_meta('daemon_settings',s.settings)
        if state['risk']['last_at'] is not None:
            s._runner.checkpoint(state['risk']['last_at'],latest_only=True)
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


def open_or_resume_session(state_root, day, *, initial_capital='10000000', send_fn=None, live_settings=None):
    date.fromisoformat(day)
    root = Path(state_root)/'daily'/day
    root.mkdir(parents=True, exist_ok=True)
    if (root/'.runtime/intraday-paper.sqlite3').exists():
        s = _resume(root, send_fn,live_settings)
        _update_latest(state_root, root)
        if _closed(s.ledger.state()):
            s.close()
            return None
        return s
    capital = _prior_capital(state_root, day, initial_capital)
    settings=merge_live_settings({'scanner_config':{'max_watch':8}},live_settings)
    s = DurablePaperSession(root, websocket_slot_available=True, initial_capital=capital,
        stream_kind='RECORDED',checkpoint_interval_seconds=4 if settings['scanner_config'].get('require_common_stock') else 0, **settings)
    s.input_sequence = 0
    s.send_fn, s.delivery_results, s.notification_errors = send_fn, [], 0
    s.set_meta('daemon_settings', s.settings)
    _update_latest(state_root, root)
    return s


def finish_session(session, adapter, *, at, end):
    try:
        # The final CLOCK still evaluates signals. Exit checks may run, but no new buys may start.
        session._runner.subscription_symbols = set()
        session.on_clock(at=at, received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
        if instant(at) >= instant(end):
            session.close_session(at=at, received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
        session._runner.checkpoint(at, latest_only=True)
        report = session.report()
        root = session.ledger.path.parent.parent
        atomic_json(root/'report.json', report)
        (root/'report.md').write_text(render_markdown_report(report), encoding='utf-8')
        if instant(at) >= instant(end) and session.send_fn is not None:
            try:
                results = notify_daily_summary(session.ledger, report, session.send_fn)
                session.delivery_results.extend(results)
                session.notification_errors += sum(row['status'] == 'FAILED' for row in results)
            except Exception:
                session.notification_errors += 1
    finally:
        try:
            if adapter is not None:
                try:
                    if adapter.socket is not None:
                        try:
                            adapter.socket.send('[]')
                        except (OSError,ConnectionError):
                            pass
                finally:
                    adapter.close()
        finally:
            session.close()


def run_trading_day(state_root, *, transport, clock=lambda: datetime.now(KST),
                    sleep=time.sleep, run_session=None, send_fn=None, stop=None, live_settings=None):
    now = instant(clock()).astimezone(KST)
    day = now.date().isoformat()
    response = transport('/api/v1/market-calendar/KR', {'date': day})
    if hasattr(response, 'status'):
        if response.status in {401, 403}:
            raise RuntimeError('SHARED_AUTH_REJECTED_NO_MUTATION')
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
    session = open_or_resume_session(state_root, day, send_fn=send_fn,live_settings=live_settings)
    if session is None:
        return {'status': 'ALREADY_CLOSED'}
    if instant(clock()) >= end:
        finish_session(session, None, at=clock(), end=end)
        return {'status': 'CLOSED_WITHOUT_COLLECTION'}
    try:
        session.on_session(normal_session_verified=True, at=clock(),
            received_monotonic=max(time.monotonic(), session.ledger.state()['last_monotonic']))
        if run_session is None:
            raise ValueError('EXPLICIT_SESSION_DRIVER_REQUIRED')
        return run_session(session, end)
    except BaseException:
        if not session._closed:
            finish_session(session, session.ws_adapter, at=clock(), end=end)
        raise


class StickyAdapter(WebSocketAdapter):
    def __init__(self, *args, min_stay_seconds=120, max_replacements=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.sticky, self.held = [], []
        self.ranked,self.session=None,None
        self.joined={}
        self.min_stay,self.max_replacements=min_stay_seconds,max_replacements

    def replace(self, topics, *, now):
        if self.pending is not None:
            return
        codes=list(dict.fromkeys(t.rsplit(':',1)[1] for t in sorted(topics)))
        if self.session is not None:
            self.held=list(dict.fromkeys(list(self.session.ledger.state()['positions'])+
                [v['symbol'] for v in self.session._runner.pending(side='SELL').values()]))
            self.ranked=list(self.session._runner.watches)
        held=list(dict.fromkeys(self.held))
        if len(held)>8:
            raise ValueError('PROTECTED_SYMBOLS_EXCEED_WS_BUDGET')
        slots=8-len(held)
        ranked=[c for c in dict.fromkeys(self.ranked if self.ranked is not None else codes) if c not in held][:slots]
        kept=[c for c in self.sticky if c not in held][:slots]
        changes=0
        for code in list(reversed(kept)):
            if code not in ranked and now-self.joined.get(code,now)>=self.min_stay and changes<self.max_replacements:
                kept.remove(code);changes+=1
        for code in ranked:
            if code in held or code in kept:
                continue
            if len(kept)<slots:
                kept.append(code)
        names=held+kept
        if self.session is not None:
            runner=self.session._runner
            changed=False
            for key,intent in runner.pending(side='BUY').items():
                if intent['symbol'] not in names:
                    runner.inactive.add(key)
                    runner.decision(self.session.ledger.state()['risk']['last_at'],intent['symbol'],
                        'SKIPPED','CANDIDATE_REMOVED',intent_id=key)
                    changed=True
            # Planned subscriptions cannot authorize entry before acknowledgement.
            runner.subscription_symbols=set(names)&{t.rsplit(':',1)[1] for t in self.active}
            if changed:
                runner.checkpoint(self.session.ledger.state()['risk']['last_at'],latest_only=True)
        self.joined={c:self.joined.get(c,now) for c in names}
        self.sticky=names
        desired = {f'{kind}:kr:{code}' for kind in ('trade', 'orderbook') for code in names}
        if desired == self.desired or self.pending is not None:
            return
        super().replace(desired, now=now)


def enqueue_job(jobs, item):
    try:
        jobs.put_nowait(item)
        return True
    except queue.Full:
        return False


def poll_websocket(adapter, monotonic, *, continue_poll=lambda:True, max_frames=512,
                   budget_clock=time.perf_counter,time_budget_seconds=.05):
    """Drain a bounded burst so a hot stream cannot bury ACK/pong behind one tick."""
    processed=0
    started=budget_clock()
    for index in range(max_frames):
        if index and budget_clock()-started>=time_budget_seconds:break
        if adapter.socket is None or not continue_poll():
            break
        try:
            raw=adapter.socket.recv(timeout=.02 if index==0 else 0)
        except TimeoutError:
            break
        except (OSError,ConnectionError):
            adapter._schedule(monotonic())
            break
        adapter.on_frame(raw,now=monotonic())
        adapter.messages.clear()
        processed+=1
    return processed


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
        return self._io(self.raw.send,text)

    def recv(self, **kwargs):
        return self._io(self.raw.recv,**kwargs)

    @staticmethod
    def _io(operation,*args,**kwargs):
        try:
            return operation(*args,**kwargs)
        except Exception as error:
            # websockets' peer-close exceptions aren't OSError subclasses.
            # Lazy classification keeps all offline tests dependency-free.
            if type(error).__module__=='websockets.exceptions' and any(
                    cls.__name__ in {'ConnectionClosed','ConnectionClosedOK','ConnectionClosedError'}
                    for cls in type(error).__mro__):
                raise ConnectionError('WEBSOCKET_CLOSED') from None
            raise

    def close(self):
        if self.closed:
            return
        try:
            try:
                self.raw.send('[]')
            except Exception:
                pass
            try:
                self._io(self.raw.close)
            except (OSError,ConnectionError):
                pass
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
        adapter.session=session
        session.ws_adapter = adapter
        adapter.replace(set(),now=mono())
        metadata=StockMetadataCache(session.ledger.meta.get('stock_reference_v1'))
        def worker():
            while not self.stop.is_set():
                try:
                    kind, query = self.jobs.get(timeout=.25)
                except queue.Empty:
                    continue
                try:
                    value = rest.fetch('/api/v1/rankings', query, now=mono(), purpose='EXIT_MONITOR') if kind != 'stocks' else self.transport('/api/v1/stocks', query)
                    if kind != 'stocks' and value['status'] == 'OK':
                        rest.recover('RANKINGS', verified=True)
                        value['entry_blocked'] = rest.entry_blocked
                    self.results.put((kind, value, query))
                except Exception:
                    self.results.put((kind, None, query))
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        next_rank, last_clock = 0, 0
        queued = set()
        deferred={}
        prices={}
        session.ledger.connection.execute('PRAGMA journal_mode=WAL')
        session.ledger.connection.execute('PRAGMA synchronous=NORMAL')
        try:
            while not self.stop.is_set() and instant(self.clock()) < end:
                current = mono()
                if current >= next_rank:
                    for kind in ('MARKET_TRADING_AMOUNT', 'MARKET_TRADING_VOLUME'):
                        if kind not in queued:
                            if enqueue_job(self.jobs,(kind, {'marketCountry': 'KR', 'duration': 'realtime', 'count': 100,
                                'excludeInvestmentCaution': 'true', 'type': kind})):
                                queued.add(kind)
                    next_rank = current + 18
                while not self.results.empty():
                    kind,value,query=self.results.get()
                    if kind=='stocks':
                        metadata.accept(query['symbols'].split(','),value,at=self.clock())
                        session.set_meta('stock_reference_v1',metadata.entries)
                        names=session.ledger.meta.get('symbol_names',{}).copy()
                        names.update({c:r['name'] for c,r in metadata.entries.items() if isinstance(r.get('name'),str)})
                        session.set_meta('symbol_names',names)
                        continue
                    queued.discard(kind)
                    if value is None:
                        value={'status':'TRANSPORT_ERROR','group':'RANKINGS','entry_blocked':True}
                    deferred[kind]=value
                for kind,value in list(deferred.items()):
                    rows=value.get('payload',{}).get('result',{}).get('rankings',[]) if value.get('status')=='OK' else []
                    codes=[r['symbol'] for r in rows]
                    needed=metadata.needed(codes,at=self.clock())
                    for begin in range(0,len(needed),200):
                        batch=needed[begin:begin+200]
                        if enqueue_job(self.jobs,('stocks',{'symbols':','.join(batch)})):
                            metadata.pending.update(batch)
                    if any(c in metadata.pending or c not in metadata.entries for c in codes):
                        continue
                    if adapter.pending is None:
                        adapter.held = list(session.ledger.state()['positions'])
                        session.on_rest('/api/v1/rankings', value, at=self.clock(), received_monotonic=mono(),
                            ranking_type=kind,risk_meta=metadata.snapshot(codes,at=self.clock()))
                        prices.update({r['symbol']:r.get('price',{}).get('lastPrice') for r in rows})
                        deferred.pop(kind)
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
                drained=0
                if adapter.socket is not None:
                    drained=poll_websocket(adapter,mono,continue_poll=lambda:instant(self.clock())<end)
                active = {t.rsplit(':', 1)[1] for t in adapter.active}
                session._runner.subscription_symbols=active
                builder.set_active(active, at=self.clock())
                if session._runner.config.scanner_config.require_common_stock:
                    for code,candidate in session._runner.watches.items():
                        state=builder.states.get(code)
                        if state and state['active']:
                            since=state['since']
                            start=since.replace(second=0,microsecond=0)
                            if since!=start:start+=timedelta(minutes=1)
                            candidate['warmup_until']=(start+timedelta(minutes=session._runner.config.strategy_config.min_candles)).isoformat()
                pending_codes = {v['symbol'] for v in session._runner.pending().values()}
                for value in buffer.drain(at=self.clock(), monotonic=mono(), pending_symbols=pending_codes):
                    frame_now = instant(self.clock())
                    stamp = instant(value['data']['timestamp'])
                    if not 0 <= (frame_now - stamp).total_seconds() <= buffer.max_age:
                        buffer.counts['STALE_OR_FUTURE_FRAME'] += 1
                        continue
                    session.on_frame(value, at=frame_now, received_monotonic=mono())
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
                        live_settings=session.settings,entry_controls=session._runner.meta.get('entry_controls',{}),
                        ranking_source_issues=list(session._runner.ranking_source_issues),
                        metadata_pending=sorted(metadata.pending),
                        selected_details={c:{**metadata.snapshot([c],at=self.clock())[c],'price':prices.get(c)} for c in session._runner.watches},
                        selection_rejections=session._runner.meta['scan_history'][-1]['rejected'] if session._runner.meta['scan_history'] else [],
                        symbol_names=session.ledger.meta.get('symbol_names', {}),
                        notification_errors=session.notification_errors, deliveries=session.delivery_results[-30:])
                    atomic_json(self.state_root/'status.json', self.stats)
                    last_clock = current
                if drained==0:self.stop.wait(.005)
            return {'status': 'STOPPED' if self.stop.is_set() else 'CLOSED'}
        finally:
            self.stop.set()
            worker_thread.join(timeout=12)
            finish_session(session, adapter, at=self.clock(), end=end)


def _lock_paper_entries(root, at):
    day = at.date().isoformat()
    daily = root/'daily'
    session_root = daily/day
    if not (session_root/'.runtime/intraday-paper.sqlite3').is_file():
        session_root = max((path.parent.parent for path in daily.glob('????-??-??/.runtime/intraday-paper.sqlite3')
                            if path.parent.parent.name < day), default=None)
        if session_root is None:
            return 'NO_OPEN_LEDGER', 0
    with closing(DaytradeLedger(session_root)) as ledger:
        state = ledger.state()
        unclosed = sum(quantity > 0 for quantity in state['positions'].values())
        if session_root.name != day or _closed(state) or state['risk']['session_date'] != day:
            return 'NO_OPEN_LEDGER', unclosed
        if RESTART_LOCK_REASON not in state['risk']['reasons'].get('DATA_DEGRADED', []):
            ledger.apply({'id': f'daemon-restart-lock-{day}', 'kind': 'LOCK',
                          'sequence': state['last_sequence'] + 1,
                          'at': max(at, instant(state['risk']['last_at'])).isoformat(),
                          'policy_version': ledger.meta['policy_version'],
                          'data': {'lock': 'DATA_DEGRADED', 'reason': RESTART_LOCK_REASON}})
    return 'APPLIED', unclosed


def _alert_restart_lock(send, record):
    if send is None:
        return 'UNAVAILABLE'
    unclosed = record.get('unclosed_positions')
    holdings = f'{unclosed}종목' if unclosed is not None else '확인 불가'
    warning = ' · 손절·시간청산 감시 중단' if unclosed != 0 else ''
    message = (f"장중 모의거래 안전 종료 ({record['error_code']})\n"
               f"최근 10분 종료 {record['window_failures']}회 · 같은 분류 {record['same_error_failures']}회\n"
               f"오늘 신규 진입과 자동 재시작을 중단했습니다.\n미청산 보유: {holdings}{warning}")
    try:
        result = send(message)
        if isinstance(result, dict) and result.get('status') == 'SENT':
            return 'SENT'
    except Exception:
        pass  # The reservation is durable even when the delivery outcome is unknown.
    return 'FAILED'


def _write_failure(root, record):
    print(canonical(record), flush=True)
    atomic_json(root/'exit.json', record)
    atomic_json(root/'status.json', record)


def _stop_after_limit(root, guard, failure_id, record, at, send):
    if record.get('ledger_lock') not in {'APPLIED', 'NO_OPEN_LEDGER'}:
        try:
            record['ledger_lock'], record['unclosed_positions'] = _lock_paper_entries(root, at)
            record.pop('ledger_lock_error', None)
        except Exception as error:
            record['ledger_lock'] = 'FAILED'
            record['ledger_lock_error'] = _error_details(error)
    if failure_id is not None:
        record['notification_status'] = _alert_restart_lock(send, record)
        guard.save(failure_id, record)
    _write_failure(root, record)
    # systemd uses Restart=on-failure. A successful exit leaves the entry lock in force.
    return 0


def run_service(root, *, stop, clock=lambda: datetime.now(KST)):
    from .service_runtime import ReadOnlyTransport, read_token, telegram_sender

    root = Path(root)
    with closing(sqlite3.connect(root/'daemon-state.sqlite3')) as db:
        guard = DaemonRestartGuard(db)
        at = instant(clock()).astimezone(KST)
        previous = guard.start(at)
        if previous is not None:
            failure_id, record = previous
            if record['entry_blocked']:
                stop.set()
                sender = None
                if failure_id is not None:
                    try:
                        sender = telegram_sender()
                    except Exception as error:
                        record['notification_error'] = _error_details(error)
                return _stop_after_limit(root, guard, failure_id, record, at, sender)
            _write_failure(root, record)
        sender = None
        try:
            transport = ReadOnlyTransport(read_token, stop)
            sender = telegram_sender()
            collector = Collector(transport, read_token, sender, stop, root, clock=clock)
            result = run_trading_day(root, transport=transport, clock=clock, send_fn=sender, stop=stop,
                sleep=lambda seconds: stop.wait(seconds), run_session=collector.run,
                live_settings=approved_live_settings())
        except (Exception, SystemExit) as error:
            if not (type(error) is RuntimeError and error.args == ('STOPPING',) and stop.is_set()):
                at = instant(clock()).astimezone(KST)
                failure_id, record = guard.fail(at, _error_details(error))
                if record['entry_blocked']:
                    stop.set()
                    return _stop_after_limit(root, guard, failure_id, record, at, sender)
                _write_failure(root, record)
                return 1
            result = {'status': 'STOPPED'}
        guard.finish()
        atomic_json(root/'exit.json', result)
        print(canonical(result), flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-root', required=True)
    args = parser.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # flock is process-scoped; a second manually started instance never shares a ledger/socket.
    import fcntl
    root = Path(args.state_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root/'service.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(canonical({'status': 'ALREADY_RUNNING'}), flush=True)
                return 0
            return run_service(root, stop=stop)
    except (OSError, sqlite3.Error, ValueError) as error:
        # A broken state store cannot safely budget another restart.
        print(canonical({'status': 'STATE_STORE_UNAVAILABLE', 'entry_blocked': True,
                         'at': datetime.now(KST).isoformat(), **_error_details(error)}), flush=True)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
