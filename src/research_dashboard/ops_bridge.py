"""Read saved portfolio and operations evidence for the dashboard."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re

# Use the dashboard's canonical engine bootstrap when only src is on PYTHONPATH.
from .daytrade_bridge import daytrade_state_path, read_public_settings

from stock_assistant.identifiers import is_korean_security_symbol
from stock_assistant.models import Decision, ScreeningResult, ThesisCard, ThesisStatus, to_json_value
from stock_assistant.news_cost import COOLDOWN, COOLDOWN_SCOPES, DAILY_LIMIT, KST, MONTHLY_LIMIT
from stock_assistant.portfolio import aggregate_holdings, review_position
from stock_assistant.research_repository import ReadOnlyResearchRepository

from .phase_b_db import DB_ERRORS, absent, dict_list, instant, readonly_database, stock_database_path, string_list

STATE_ERRORS = (*DB_ERRORS, RuntimeError)
DAEMON_STATES = frozenset({
    "OBSERVING", "FAILED", "RESTART_LOCKED", "STATE_STORE_UNAVAILABLE", "MARKET_HOLIDAY_OR_NON_STANDARD_SESSION",
    "STOPPED_BEFORE_OPEN", "AFTER_MARKET_NO_COLLECTION", "ALREADY_CLOSED", "CLOSED_WITHOUT_COLLECTION",
    "STOPPED", "CLOSED", "ALREADY_RUNNING",
})
DAEMON_CODES = {
    "error_code": frozenset({"AUTH", "NETWORK", "STATE", "DATA", "UNKNOWN"}),
    "ledger_lock": frozenset({"APPLIED", "NO_OPEN_LEDGER", "FAILED"}),
    "notification_status": frozenset({"RESERVED", "SENT", "FAILED", "UNAVAILABLE"}),
}
DAEMON_COUNTS = ("socket_connections", "peak_active_sockets", "active_sockets", "ws_frames",
                 "notification_errors", "window_failures", "same_error_failures", "unclosed_positions")
FINANCIAL_CHECKS = (
    "original_annual_income_equal", "original_annual_ocf_equal", "original_interim_cumulative_equal",
    "previous_comparative_equal_separate_prior_year_report", "original_ttm_formula_equal", "stored_annual_income_equal",
    "stored_ttm_income_equal", "stored_operating_income_equal", "stored_annual_ocf_equal", "stored_periods_equal",
    "stored_receipt_links_equal",
)
VERIFICATION_STATES = ("BLOCKED_NO_POINT_IN_TIME_DATASET", "THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY")


def _saved_theses(repository, symbol, now):
    with repository._connect() as db:
        rows = db.execute("SELECT thesis_id, payload_json FROM thesis_cards WHERE symbol=?", (symbol,)).fetchall()
    theses = []
    for identifier, raw in rows:
        payload = json.loads(raw)
        created_at = instant(payload["created_at"])
        if payload["symbol"] != symbol or payload["thesis_id"] != identifier:
            raise ValueError("thesis identity mismatch")
        if created_at <= now:
            theses.append(ThesisCard(
                identifier, symbol, created_at, tuple(string_list(payload["facts"])),
                tuple(string_list(payload["assumptions"])), tuple(string_list(payload["invalidation_conditions"])),
                tuple(string_list(payload["additional_check_conditions"])), ThesisStatus(payload["status"]),
            ))
    return sorted(theses, key=lambda thesis: (thesis.created_at, thesis.thesis_id), reverse=True)


def _last_screening(repository, symbol, now):
    with repository._connect() as db:
        row = db.execute(
            "SELECT as_of, payload_json FROM screening_results WHERE symbol=? AND as_of<=? "
            "ORDER BY as_of DESC, report_id DESC LIMIT 1",
            (symbol, now.astimezone(timezone.utc).isoformat()),
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    as_of = instant(payload["as_of"])
    if payload["symbol"] != symbol or as_of != instant(row["as_of"]) or as_of > now:
        raise ValueError("screening identity mismatch")
    return ScreeningResult(symbol, as_of, Decision(payload["decision"]), Decimal(payload["score"]),
                           (), (), tuple(string_list(payload["checks"])))


def _position_review(repository, position, theses, now):
    unavailable = {"symbol": position.symbol, "present": False}
    if not theses:
        return {**unavailable, "reason": "THESIS_UNAVAILABLE"}
    thesis = theses[0]
    if thesis.status in {ThesisStatus.DRAFT, ThesisStatus.CLOSED}:
        return {**unavailable, "reason": "THESIS_REVIEW_REQUIRED"}
    screening = _last_screening(repository, position.symbol, now)
    if screening is None:
        return {**unavailable, "reason": "SCREENING_UNAVAILABLE"}
    bars = repository.bars_for(position.symbol, as_of=now.astimezone(KST), limit=1)
    if not bars or bars[-1].close <= 0:
        return {**unavailable, "reason": "PRICE_UNAVAILABLE"}
    close = bars[-1]
    return {
        "present": True,
        **to_json_value(review_position(position, current_price=close.close, thesis=thesis, screening=screening)),
        "price_date": close.trade_date.isoformat(),
        "screening_at": screening.as_of.isoformat(),
        "thesis_id": thesis.thesis_id,
    }


def _holdings(root, now):
    unavailable = {"items": [], "positions": [], "theses": [], "reviews": []}
    try:
        repository = ReadOnlyResearchRepository(stock_database_path(root))
        holdings = repository.list_holdings()
        positions = aggregate_holdings(holdings)
    except STATE_ERRORS:
        return {**absent("HOLDINGS_UNAVAILABLE"), **unavailable}

    theses, reviews = [], []
    for position in positions:
        try:
            saved = _saved_theses(repository, position.symbol, now)
            theses.extend(saved)
            reviews.append(_position_review(repository, position, saved, now))
        except STATE_ERRORS:
            reviews.append({"symbol": position.symbol, **absent("REVIEW_INPUT_UNAVAILABLE")})
    return {"present": True, **to_json_value({
        "items": holdings, "positions": positions, "theses": theses, "reviews": reviews,
    })}


def _news_budget(root, now):
    setting = os.getenv("STOCK_NEWS_USAGE_FILE", "").strip()
    path = Path(root) / (setting or ".runtime/news-api-usage.sqlite3")
    today = now.astimezone(KST).date()
    day, month = today.isoformat(), today.strftime("%Y-%m")
    limits = {"daily_limit": DAILY_LIMIT, "monthly_limit": MONTHLY_LIMIT, "policy_status": "UNKNOWN"}
    unavailable = {**absent("NEWS_USAGE_STATE_UNAVAILABLE"), **limits}
    try:
        if path.is_symlink():
            return unavailable
        with readonly_database(path) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT day,month,daily_used,monthly_used,last_reserved_at,halted_month "
                             "FROM news_api_budget WHERE id=1").fetchone()
            if row is None:
                return unavailable
            old_day, old_month, daily, monthly, last, halted = row
            last_by_scope = {"DEFAULT": instant(last)}
            if db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='news_api_cooldowns'").fetchone():
                for scope, recorded_at in db.execute("SELECT scope,last_reserved_at FROM news_api_cooldowns"):
                    if scope not in COOLDOWN_SCOPES or scope == "DEFAULT":
                        return unavailable
                    last_by_scope[scope] = instant(recorded_at)
        old_date = date.fromisoformat(old_day)
        if (old_day != old_date.isoformat() or old_month != old_day[:7] or old_date > today
                or max(last_by_scope.values()) > now
                or any(type(count) is not int or count < 0 for count in (daily, monthly)) or monthly < daily
                or halted not in ("", old_month)):
            return unavailable
        cooldowns = {}
        for scope in sorted(COOLDOWN_SCOPES):
            reserved = last_by_scope.get(scope)
            available = reserved + COOLDOWN if reserved is not None else None
            cooldowns[scope] = {
                "status": "COOLDOWN" if available is not None and available > now else "READY",
                "last_reserved_at": reserved.isoformat() if reserved is not None else None,
                "available_at": available.isoformat() if available is not None else None,
            }
        return {
            "present": True, **limits, "day": day, "month": month,
            "daily_used": daily if old_day == day else 0, "monthly_used": monthly if old_month == month else 0,
            "provider_halted": halted == month, "cooldowns": cooldowns,
        }
    except STATE_ERRORS:
        return unavailable


def _local_json(path, max_bytes):
    if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("unsafe state file")
    with path.open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("state file too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("state object required")
    return value


def _token_state(root, now):
    configured = os.getenv("DAYTRADE_TOKEN_CACHE", "").strip()
    if not configured:
        return {"token_status": "UNKNOWN"}
    cache = Path(root).resolve() / configured / "toss-token.json"
    try:
        if os.name != "nt" and cache.stat().st_mode & 0o077:
            raise ValueError("token cache permissions")
        record = _local_json(cache, 16_384)
        expires = record.get("valid_until")
        client_hash = record.get("client_hash")
        if (set(record) != {"version", "client_hash", "token", "valid_until"} or type(record.get("version")) is not int
                or record["version"] != 1 or type(expires) not in (int, float) or not math.isfinite(expires)
                or not isinstance(client_hash, str) or re.fullmatch(r"[a-f0-9]{64}", client_hash) is None
                or not isinstance(record.get("token"), str) or not 1 <= len(record["token"]) <= 8192):
            raise ValueError("token expiry unavailable")
        valid_until = datetime.fromtimestamp(expires, tz=timezone.utc)
        remaining = valid_until - now
        status = "EXPIRED" if remaining <= timedelta(0) else "EXPIRING" if remaining < timedelta(seconds=120) else "VALID"
        return {"token_status": status, "token_valid_until": valid_until.isoformat(), "token_status_source": "CACHE_EXPIRY"}
    except STATE_ERRORS:
        return {"token_status": "UNKNOWN"}


def _daytrade_status(root, now):
    token = _token_state(root, now)
    result = {"orders_enabled": False, **token}
    try:
        saved = _local_json(daytrade_state_path(root) / "status.json", 1_000_000)
        status = saved.get("status")
        if not isinstance(status, str) or status not in DAEMON_STATES:
            raise ValueError("unknown daemon status")
        result.update(present=True, status=status)
        if saved.get("at") is not None:
            result["at"] = instant(saved["at"]).isoformat()
        for key in DAEMON_COUNTS:
            value = saved.get(key)
            if type(value) is int and 0 <= value <= 10**12:
                result[key] = value
        for key, allowed in DAEMON_CODES.items():
            value = saved.get(key)
            if isinstance(value, str) and value in allowed:
                result[key] = value
        if type(saved.get("entry_blocked")) is bool:
            result["entry_blocked"] = saved["entry_blocked"]
        capital = saved.get("initial_capital")
        if isinstance(capital, str) and re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,12})?", capital):
            result["initial_capital"] = capital
        settings = read_public_settings(saved.get("live_settings"))
        if settings:
            result["live_settings"] = settings
        return result
    except STATE_ERRORS:
        return {**absent("DAYTRADE_STATUS_UNAVAILABLE"), "orders_enabled": False, **token}


def _object(value):
    return value if isinstance(value, dict) else {}


def _fields(value, keys, kind):
    return {key: value[key] for key in keys if type(value.get(key)) is kind}


def _public_day(value):
    try:
        parsed = date.fromisoformat(value)
        return value if value == parsed.isoformat() else None
    except (ValueError, TypeError):
        return None


def _public_instant(value):
    try:
        return instant(value).isoformat()
    except (ValueError, TypeError):
        return None


def _coverage_inventory(value):
    source = _object(value)
    tables = _object(source.get("tables"))
    price = _object(tables.get("ohlcv"))
    financial = _object(tables.get("financial_snapshots"))
    price_summary = _fields(price, ("rows", "symbols", "distinct_dates"), int)
    bounds = price.get("trade_date_range")
    if isinstance(bounds, list) and len(bounds) == 2:
        price_summary["trade_date_range"] = [_public_day(bound) for bound in bounds]
    known_tables = {}
    if price_summary:
        known_tables["ohlcv"] = price_summary
    if financial_summary := _fields(financial, ("rows", "symbols"), int):
        known_tables["financial_snapshots"] = financial_summary
    result = _fields(source, ("read_only", "point_in_time_certified"), bool)
    if known_tables:
        result["tables"] = known_tables
    return result


def _price_verification(value):
    source = _object(value)
    results = []
    for row in dict_list(source.get("results")):
        start, end = _public_day(row.get("start")), _public_day(row.get("end"))
        if is_korean_security_symbol(row.get("symbol")) and start and end and start <= end:
            results.append({
                "symbol": row["symbol"], "start": start, "end": end,
                **_fields(row, ("distinct_trading_dates", "calculation_dates_checked"), int),
                **_fields(row, ("source_features_equal_independent_formulas",), bool),
            })
    result = _fields(source, ("calculation_dates_total",), int)
    if as_of := _public_instant(source.get("as_of")):
        result["as_of"] = as_of
    if results:
        result["results"] = results
    return result


def _financial_verification(value):
    source = _object(value)
    if not is_korean_security_symbol(source.get("symbol")):
        return {}
    result = {"symbol": source["symbol"], **_fields(source, ("all_equal",), bool)}
    if source.get("division") in ("CFS", "OFS"):
        result["division"] = source["division"]
    if observed_at := _public_instant(source.get("observed_at")):
        result["observed_at"] = observed_at
    if checks := _fields(_object(source.get("checks")), FINANCIAL_CHECKS, bool):
        result["checks"] = checks
    return result


def _verification_summary(status):
    source = _object(status)
    if source.get("present") is False:
        return absent("VERIFICATION_UNAVAILABLE")
    result = _fields(source, ("hashes_match", "production_db_accessed", "production_db_changed"), bool)
    if source.get("mode") == "SYNTHETIC":
        result["mode"] = "SYNTHETIC"
    data_status = source.get("data_status")
    if data_status in VERIFICATION_STATES:
        result["data_status"] = data_status
    if checked := _public_instant(source.get("checked_at_kst")):
        result["checked_at_kst"] = checked
    head = source.get("source_head")
    if isinstance(head, str) and re.fullmatch(r"[a-f0-9]{40}", head):
        result["source_head"] = head
    for key, summary in (("oracle_inventory", _coverage_inventory), ("actual_price_validation", _price_verification),
                         ("actual_financial_validation", _financial_verification)):
        if value := summary(source.get(key)):
            result[key] = value
    return result or absent("VERIFICATION_UNAVAILABLE")


def get_ops_overview(root, status, *, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    holdings = _holdings(root, now)
    news = _news_budget(root, now)
    daytrade = _daytrade_status(root, now)
    verification = _verification_summary(status)
    present = holdings["present"] or news["present"] or daytrade["present"] or verification.get("present") is not False
    return {
        **({"present": True} if present else absent("OPS_SOURCES_UNAVAILABLE")),
        "holdings": holdings,
        "news_budget": news,
        "daytrade": daytrade,
        "verification": verification,
    }
