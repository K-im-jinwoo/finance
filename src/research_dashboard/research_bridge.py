"""Saved unified research reports and chart data pinned to a report snapshot."""
from datetime import date, timedelta, timezone
import hashlib
import json
import math
import re

from stock_assistant.candidate_charts import technical_analysis
from stock_assistant.chart_analysis import _sma_series, compute_full_chart_analysis
from stock_assistant.financial_research import WEIGHTS
from stock_assistant.indicators import price_features, wilder_atr
from stock_assistant.models import to_json_value
from stock_assistant.repository import _bar_from_dict

from .phase_b_db import (
    DB_ERRORS, REPORT_MODES, UNIFIED_ID, absent, count_values, dict_list, instant, read_report,
    read_report_summaries, readonly_database, safe_evidence, safe_url, scalar_fields,
    stock_database_path, string_list, valid_id,
)


_KIND = "unified_report"
_CONTRACT = "UNIFIED_RESEARCH_V1"
_SYMBOL = re.compile(r"[0-9]{6}")
_KST = timezone(timedelta(hours=9))
_PERIODS = (5, 20, 60, 120)
_DISPLAY_BARS = 120
_HISTORY_BARS = _DISPLAY_BARS + max(_PERIODS) - 1
_STAGE_FIELDS = {
    "archive": "status requested processed completed",
    "calendar": "status input_id previous_session",
    "financials": "status universe_size selected processed requests source_read_only",
    "material_archive": "status requested processed completed",
    "news_budget": "reserved_calls actual_calls",
    "official_events": "status source_read_only events_automatically_verified",
    "prices": "status target_date missing_market fallback_session required_session",
    "research_focus": "status limit",
    "risk_coverage": "status completed",
}
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,119}")
_NEWS_REVIEW = "MATERIAL_EVENT_REVIEW_REQUIRED"
_PRICE_METRICS = "close sma20 sma60 return5 return20 drawdown60 volume_ratio20 average_value20 atr14"
_REPORT_FIELDS = (
    "report_id contract policy_version policy_status as_of score_formula source_read_only orders_enabled "
    "paper_account_eligible recommendation_scope coverage_scope expected_price_session market_calendar_input_id "
    "policy_mode price_session_basis"
)
_CANDIDATE_FIELDS = (
    "symbol name action_decision label financial_score news_score risk_penalty total_score tie_breaker "
    "liquidity_tier in_cooldown financial_evaluation_id recent_news_complete risk_history_complete rank "
    "price_input_fingerprint news_reference_date"
)


def list_research_reports(root):
    return read_report_summaries(root, kind=_KIND, contract=_CONTRACT, id_pattern=UNIFIED_ID)


def get_research_report(root, report_id):
    if not valid_id(UNIFIED_ID, report_id):
        return absent("INVALID_REPORT_ID")
    result = read_report(root, report_id, kind=_KIND, contract=_CONTRACT)
    if result["present"]:
        result["report"] = _safe_report(result["report"])
    return result


def _safe_report(report):
    result = scalar_fields(report, _REPORT_FIELDS)
    if report.get("mode") in REPORT_MODES:
        result["mode"] = report["mode"]
    for field in ("candidates", "evaluations", "research_queue"):
        if field in report:
            result[field] = [_safe_candidate(row) for row in dict_list(report[field])]
    for field in ("decision_counts", "pending_reason_counts"):
        if field in report:
            result[field] = count_values(report[field])
    for field, keys in (
        ("thresholds", "financial total"),
        ("research_windows", "end recent_start risk_start official_start"),
        ("research_progress", "financial_qualified news_scored risk_history_completed"),
        ("candidate_comparison", "previous_as_of"),
    ):
        value = report.get(field)
        if isinstance(value, dict):
            result[field] = scalar_fields(value, keys)
    if "research_progress" in result:
        result["research_progress"]["pending_reason_counts"] = count_values(
            report["research_progress"].get("pending_reason_counts"))
    if "candidate_comparison" in result and isinstance(report["candidate_comparison"].get("changes"), dict):
        result["candidate_comparison"]["changes"] = {
            symbol: scalar_fields(change, "new previous_total_score component_scores_changed previous_decision")
            for symbol, change in report["candidate_comparison"]["changes"].items() if valid_id(_SYMBOL, symbol)
        }
    if isinstance(report.get("refresh_stages"), dict):
        result["refresh_stages"] = _safe_stages(report["refresh_stages"])
    return result


def _safe_stages(stages):
    result = {}
    for stage, fields in _STAGE_FIELDS.items():
        value = stages.get(stage)
        if not isinstance(value, dict):
            continue
        result[stage] = scalar_fields(value, fields)
        if valid_id(_ERROR_CODE, value.get("error_code")):
            result[stage]["error_code"] = value["error_code"]
    return result


def _safe_candidate(row):
    result = scalar_fields(row, _CANDIDATE_FIELDS)
    for field in ("reasons", "warnings", "pending_checks", "advisories", "source_input_ids"):
        if field in row:
            result[field] = string_list(row[field])
    if "pending_checks" in result:
        result["pending_checks"] = [
            _NEWS_REVIEW if check.startswith(_NEWS_REVIEW + ":") and not safe_url(check.split(":", 1)[1])
            else check for check in result["pending_checks"]
        ]
    if "financial_source_url" in row:
        result["financial_source_url"] = safe_url(row["financial_source_url"])
    breakdown = row.get("financial_breakdown")
    if isinstance(breakdown, dict):
        result["financial_breakdown"] = {
            key: scalar_fields(breakdown[key], "points maximum status reason") for key in WEIGHTS if key in breakdown
        }
    price = row.get("price")
    if isinstance(price, dict):
        result["price"] = scalar_fields(price, "status strategy session")
        for field in ("reasons", "entry_conditions", "invalidation_conditions"):
            if field in price:
                result["price"][field] = string_list(price[field])
        if "metrics" in price:
            result["price"]["metrics"] = scalar_fields(price["metrics"], _PRICE_METRICS)
    signal = row.get("chart_signal")
    if isinstance(signal, dict):
        result["chart_signal"] = scalar_fields(signal, "grade grade_label breakout_price support_price stop_loss")
        for field in ("reasons", "annotations"):
            if field in signal:
                result["chart_signal"][field] = string_list(signal[field])
        if "primary_pattern" in signal:
            primary = signal["primary_pattern"]
            result["chart_signal"]["primary_pattern"] = None if primary is None else scalar_fields(
                primary, "code name_ko status")
    if "news_events" in row:
        result["news_events"] = [
            scalar_fields(event, "event_id points status score_target category title direction")
            for event in dict_list(row["news_events"])
        ]
    for field in ("recent_headlines", "risk_headlines", "recent_dart_filings"):
        if field in row:
            result[field] = [safe_evidence(item) for item in dict_list(row[field])]
    return result


def get_research_chart(root, symbol, report_id):
    if not valid_id(_SYMBOL, symbol):
        return absent("INVALID_SYMBOL")
    if not valid_id(UNIFIED_ID, report_id):
        return absent("INVALID_REPORT_ID")
    result = read_report(root, report_id, kind=_KIND, contract=_CONTRACT)
    if not result["present"]:
        return result
    report = result["report"]
    rows = (row for field in ("candidates", "evaluations", "research_queue")
            for row in dict_list(report.get(field)))
    candidate = next((row for row in rows if row.get("symbol") == symbol), None)
    if candidate is None:
        return absent("SYMBOL_NOT_IN_REPORT")
    try:
        return _chart(root, report, candidate, symbol)
    except FileNotFoundError:
        return absent("DATABASE_MISSING")
    except DB_ERRORS:
        return absent("DATABASE_UNAVAILABLE")


def _chart(root, report, candidate, symbol):
    as_of = instant(report["as_of"])
    market_day = as_of.astimezone(_KST).date()
    price = scalar_fields(candidate.get("price"), "strategy session")
    exact_session = date.fromisoformat(price["session"]) if price.get("session") else None
    expected = report.get("expected_price_session")
    cutoff = exact_session or (date.fromisoformat(expected) if expected else market_day)
    if cutoff > market_day:
        return absent("INVALID_PRICE_SESSION")
    history = _read_chart_bars(stock_database_path(root), symbol, as_of, cutoff)
    if not history:
        return absent("BARS_NOT_FOUND")
    bars = history[-_DISPLAY_BARS:]
    session = bars[-1].trade_date
    if exact_session is not None and session != exact_session:
        return absent("PRICE_SESSION_INCOMPLETE")
    expected_fingerprint = candidate.get("price_input_fingerprint")
    if expected_fingerprint:
        fingerprint = hashlib.sha256(json.dumps(to_json_value(bars), sort_keys=True).encode()).hexdigest()
        if fingerprint != expected_fingerprint:
            return absent("REPORT_PRICE_INPUT_MISMATCH")

    full = len(bars) >= 61
    analysis = compute_full_chart_analysis(bars) if full else None
    technical = technical_analysis({
        "bars": bars, "metrics": to_json_value(price_features(bars)), "session": session,
        "stored_strategy": price.get("strategy"),
    }) if full else None
    levels = analysis["support_resistance"] if full else {}
    return {
        "present": True, "report_id": report["report_id"], "symbol": symbol,
        "as_of": report["as_of"], "price_session": session.isoformat(),
        "bars": _chart_bars(bars, analysis, history[:-_DISPLAY_BARS]),
        "support": levels.get("supports", []), "resistance": levels.get("resistances", []),
        "analysis": analysis, "technical": technical, "analysis_available": full,
    }


def _read_chart_bars(path, symbol, as_of, cutoff):
    # Market date and observation time are independent cutoffs. Apply both before the row limit.
    observed = as_of.astimezone(timezone.utc).isoformat()
    with readonly_database(path) as db:
        rows = db.execute("""
            SELECT bar.trade_date, bar.observed_at, bar.payload_json FROM ohlcv AS bar
            WHERE bar.symbol=? AND bar.trade_date<=? AND bar.observed_at<=?
            AND bar.observed_at=(
                SELECT MAX(history.observed_at) FROM ohlcv AS history
                WHERE history.symbol=bar.symbol AND history.trade_date=bar.trade_date AND history.observed_at<=?
            ) ORDER BY bar.trade_date DESC LIMIT ?
            """, (symbol, cutoff.isoformat(), observed, observed, _HISTORY_BARS)).fetchall()
    bars = []
    for trade_date, observed_at, raw in rows:
        try:
            bar = _bar_from_dict(json.loads(raw))
            if (bar.symbol != symbol or bar.trade_date.isoformat() != trade_date or bar.trade_date > cutoff
                    or bar.observed_at.isoformat() != observed_at or bar.observed_at > as_of
                    or any(not math.isfinite(point) or point <= 0 for point in (bar.open, bar.high, bar.low, bar.close))):
                raise ValueError("invalid stored OHLCV data")
        except DB_ERRORS:
            if len(bars) < _DISPLAY_BARS:
                raise
            # An unusable older row ends the contiguous SMA warm-up history.
            break
        bars.append(bar)
    return bars[::-1]


def _chart_bars(bars, analysis, warmup):
    closes = [float(bar.close) for bar in bars]
    warmup_closes = [float(bar.close) for bar in warmup]
    sma_inputs = warmup_closes + closes
    averages = {period: _sma_series(sma_inputs, period)[len(warmup):] for period in _PERIODS}
    rsi = analysis["rsi14"]["series"] if analysis else [None] * len(bars)
    listed = []
    for index, bar in enumerate(bars):
        # Match the engine's trailing 61-bar ATR seed on each eligible session.
        atr = wilder_atr(bars[max(0, index - 60):index + 1]) if analysis and index >= 14 else None
        previous_volume = sum(item.volume for item in bars[index - 20:index]) if analysis and index >= 20 else 0
        listed.append({
            "date": bar.trade_date.isoformat(), "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": closes[index], "volume": bar.volume,
            "sma5": averages[5][index], "sma20": averages[20][index], "sma60": averages[60][index],
            "sma120": averages[120][index], "rsi14": rsi[index], "atr14": float(atr) if atr is not None else None,
            "volume_ratio": bar.volume * 20 / previous_volume if previous_volume else None,
        })
    return listed
