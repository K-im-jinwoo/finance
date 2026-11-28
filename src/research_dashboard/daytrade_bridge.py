"""Local daytrade reports and opt-in synthetic samples; no live market wiring."""
from __future__ import annotations

import json
import os
from datetime import date as calendar_date
from pathlib import Path
import re
import sqlite3
import sys

try:
    from stock_assistant.daytrade import MODE, build_trading_report, render_markdown_report, run_paper_session
except ModuleNotFoundError as exc:
    if exc.name != "stock_assistant":
        raise
    # Resolve relative to this package, never to an untrusted supplied root/CWD.
    engine_source = Path(__file__).resolve().parents[2] / "engine/src"
    if not (engine_source / "stock_assistant/__init__.py").is_file():
        raise
    sys.path.insert(0, str(engine_source))
    from stock_assistant.daytrade import MODE, build_trading_report, render_markdown_report, run_paper_session

from stock_assistant.daytrade.ledger import APPLICATION_ID, DaytradeLedger, FixtureCosts, isolated_path
from stock_assistant.daytrade.market import QualityPolicy
from stock_assistant.daytrade.stream import run_observed_stream

SESSION_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
CONFIG_KEYS = {
    "quality": ("candle_max_age_seconds", "quote_max_age_seconds", "ranking_max_age_seconds"),
    "scanner_config": ("max_watch", "require_common_stock", "max_price", "min_price", "min_turnover",
                       "max_spread_bps", "min_market_cap", "max_market_cap", "ranking_max_age_seconds",
                       "quote_max_age_seconds"),
    "strategy_config": ("profile", "min_candles", "min_bid_volume", "min_bid_notional", "quote_max_age_seconds",
                        "candle_max_age_seconds", "entry_start_hhmm", "min_hold_seconds_for_weak_exit",
                        "volume_spike_ratio", "max_ask_bid_volume_ratio", "breakout_lookback_bars", "max_spread_bps",
                        "take_profit_bps", "stop_loss_bps", "weak_exit_min_vwap_gap_bps",
                        "trailing_stop_activation_bps", "trailing_stop_retrace_bps", "weak_exit_enabled",
                        "partial_take_profit_bps", "partial_take_profit_ratio",
                        "post_partial_breakeven_bps", "post_partial_trailing_retrace_bps"),
}


def read_public_settings(settings):
    """Read only known paper configuration keys and their expected scalar forms."""
    if not isinstance(settings, dict):
        return {}
    result = {key: settings[key] for key in ("max_daily_entries", "reentry_cooldown_seconds")
              if type(settings.get(key)) is int and 0 <= settings[key] <= 1_000_000}
    for section, keys in CONFIG_KEYS.items():
        values = settings.get(section)
        if not isinstance(values, dict):
            continue
        public = {key: values[key] for key in keys if key in values and _public_setting(key, values[key])}
        if public:
            result[section] = public
    return result


def _public_setting(key, value):
    if key == "profile":
        return value == MODE
    if key == "entry_start_hhmm":
        return isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is not None
    if key in {"require_common_stock", "weak_exit_enabled"}:
        return type(value) is bool
    return (type(value) is int and 0 <= value <= 10**20
            or isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,12})?", value) is not None)


def daytrade_state_path(root, state_root=None):
    root = Path(root).resolve()
    configured = state_root if state_root is not None else os.getenv("DAYTRADE_STATE_ROOT")
    return root / (Path(configured) if configured else Path(".runtime/daytrade"))


def _valid_session_date(value):
    if not isinstance(value, str) or SESSION_DATE.fullmatch(value) is None:
        return False
    try:
        calendar_date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _unredirected_path(path):
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("redirected daytrade path")
    return path


def _session_root(daily, day):
    session = _unredirected_path(daily / day)
    ledger = isolated_path(session)
    if not ledger.is_file():
        raise FileNotFoundError("daytrade ledger missing")
    for suffix in ("-wal", "-shm", "-journal"):
        auxiliary = _unredirected_path(ledger.with_name(ledger.name + suffix))
        if auxiliary.exists() and (not auxiliary.is_file() or auxiliary.stat().st_nlink != 1):
            raise ValueError("linked or non-file sqlite sidecar")
    return session


def _inactive_wal_identity(path):
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    with path.open("rb") as stream:
        header = stream.read(20)
    if header[:16] != b"SQLite format 3\0" or header[18:20] != b"\x02\x02":
        return None
    active = wal.exists(), shm.exists()
    if all(active):
        return None
    if any(active):
        raise ValueError("incomplete WAL sidecars")
    status = path.stat()
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def list_daytrade_sessions(root, state_root=None):
    sessions = []
    try:
        daily = _unredirected_path(daytrade_state_path(root, state_root) / "daily")
        for candidate in daily.iterdir():
            if not _valid_session_date(candidate.name):
                continue
            try:
                _session_root(daily, candidate.name)
            except (OSError, RuntimeError, ValueError):
                continue
            sessions.append(candidate.name)
    except (OSError, RuntimeError, ValueError):
        return {"present": False, "reason": "DAYTRADE_SESSIONS_UNAVAILABLE", "sessions": [], "latest": None}
    sessions.sort(reverse=True)
    return {"present": True, "sessions": sessions, "latest": sessions[0] if sessions else None}


class _ReadOnlyLedger(DaytradeLedger):
    """Use the existing report/audit methods with a genuinely read-only SQLite URI.

    Keep the engine ledger's constructor/execution contract unchanged. Mirror its
    schema and metadata checks here; a read transaction holds a coherent snapshot
    even if a separately invoked local CLI is writing the same ledger.
    """
    def __init__(self, root, *, inactive_wal=False):
        self.path = isolated_path(root)
        uri = self.path.as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri + ("&immutable=1" if inactive_wal else ""), uri=True)
        try:
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("BEGIN")
            if self.connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("daytrade database identity mismatch")
            self.meta = {key: json.loads(value) for key, value in self.connection.execute("SELECT key,value FROM meta")}
            if self.meta.get("schema") != 1 or self.meta.get("mode") != MODE:
                raise ValueError("unsupported ledger or execution mode")
            self.quality = QualityPolicy(**self.meta["quality"])
            self.costs = FixtureCosts(**self.meta["fixture_costs"]) if self.meta["fixture_costs"] is not None else None
            self.audit()
        except Exception:
            self.connection.close()
            raise


def _sample_path(root):
    # A foreign/temp root must supply its own fixture. No implicit fallback to a
    # different checkout's sample, which would hide an incomplete installation.
    for relative in ("datasets/daytrade_observed_stream_sample.json", "datasets/daytrade_bot_sample.json",
                     "engine/tests/fixtures/daytrade_observed_stream_sample.json", "engine/tests/fixtures/daytrade_bot_sample.json"):
        path = Path(root) / relative
        if path.is_file():
            return path
    return None


def read_daytrade_report(root, *, dated=False):
    path = isolated_path(root)
    result = {"present": path.is_file(), "orders_enabled": False, "paper_ready": False,
              "sample_available": not dated and _sample_path(root) is not None, "report": None, "markdown": None}
    if not result["present"]:
        return result
    # A fully checkpointed WAL-mode database otherwise creates journal sidecars
    # even when opened read-only. Active WALs still need SQLite's live snapshot.
    identity = _inactive_wal_identity(path) if dated else None
    ledger = _ReadOnlyLedger(root, inactive_wal=identity is not None)
    try:
        report = build_trading_report(ledger)
        if dated and (settings := read_public_settings(ledger.meta.get("daemon_settings"))):
            result["live_settings"] = settings
        result.update(report=report, markdown=render_markdown_report(report))
    finally:
        ledger.close()
    if identity is not None and _inactive_wal_identity(path) != identity:
        raise ValueError("daytrade ledger changed during inactive WAL read")
    return result


def read_daytrade_session(root, date, state_root=None):
    unavailable = {"present": False, "orders_enabled": False, "paper_ready": False,
                   "sample_available": False, "report": None, "markdown": None}
    if not _valid_session_date(date):
        return {**unavailable, "reason": "DAYTRADE_DATE_INVALID"}
    try:
        daily = _unredirected_path(daytrade_state_path(root, state_root) / "daily")
        session = _session_root(daily, date)
    except (OSError, RuntimeError, ValueError):
        return {**unavailable, "reason": "DAYTRADE_SESSION_UNAVAILABLE"}
    try:
        result = read_daytrade_report(session, dated=True)
        if not result["present"]:
            return {**unavailable, "reason": "DAYTRADE_SESSION_UNAVAILABLE"}
        sessions = result["report"]["sessions"]
        if not sessions:
            return {**unavailable, "reason": "DAYTRADE_SESSION_NOT_RECORDED"}
        if set(sessions) != {date}:
            return {**unavailable, "reason": "DAYTRADE_SESSION_DATE_MISMATCH"}
        return result
    except (OSError, RuntimeError, ValueError, ArithmeticError, TypeError, LookupError, AttributeError, sqlite3.Error):
        return {**unavailable, "reason": "DAYTRADE_REPORT_UNAVAILABLE"}


def simulate_sample_daytrade(root):
    path = isolated_path(root)
    if path.exists():
        raise FileExistsError("daytrade ledger already exists")
    sample = _sample_path(root)
    if sample is None:
        raise FileNotFoundError("daytrade sample unavailable")
    if sample.stat().st_size > 10_000_000:
        raise ValueError("bounded daytrade sample required")
    dataset = json.loads(sample.read_text(encoding="utf-8"))
    runner = run_observed_stream if sample.name == "daytrade_observed_stream_sample.json" else run_paper_session
    runner(dataset, root)
    return read_daytrade_report(root)
