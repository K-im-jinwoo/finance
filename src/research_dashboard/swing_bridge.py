"""Read saved swing publications and stored performance records."""
from stock_assistant.backtest import SWING_POLICY
from stock_assistant.models import to_json_value
from stock_assistant.performance import summarize_performance
from stock_assistant.research_repository import ReadOnlyResearchRepository

from .phase_b_db import (
    DB_ERRORS, PERFORMANCE_ID, REPORT_MODES, SWING_ID, absent, dict_list, read_report,
    read_report_summaries, safe_evidence, scalar_fields, stock_database_path, valid_id,
)


_KIND = "swing_report"
_DURATIONS = ("ONE_OFF", "POTENTIALLY_PERSISTENT", "UNCLEAR")
_CANDIDATE_FIELDS = (
    "symbol name rank turnover turnover_multiple sma20 close return_5d market_cap "
    "news_reference_date action_decision expected_holding_period"
)


def list_swing_reports(root):
    return read_report_summaries(root, kind=_KIND, contract=SWING_POLICY, id_pattern=SWING_ID)


def get_swing_report(root, report_id):
    if not valid_id(SWING_ID, report_id):
        return absent("INVALID_REPORT_ID")
    result = read_report(root, report_id, kind=_KIND, contract=SWING_POLICY)
    if not result["present"]:
        return result
    report = result["report"]
    safe = scalar_fields(report, "report_id contract policy_version as_of price_session status reason "
                                "orders_enabled news_checked_count")
    if report.get("mode") in REPORT_MODES:
        safe["mode"] = report["mode"]
    safe["candidates"] = [_safe_candidate(row) for row in dict_list(report.get("candidates"))]
    return {"present": True, "report": safe}


def _safe_candidate(row):
    candidate = scalar_fields(row, _CANDIDATE_FIELDS)
    if "price" in row:
        candidate["price"] = scalar_fields(row["price"], "strategy")
    articles = dict_list(row.get("news_evidence"))
    candidate["news_evidence"] = [safe_evidence(article) for article in articles]
    # Swing delivery currently calculates Hermes duration in memory. A report
    # without a saved classification cannot establish what Hermes reported.
    duration = row.get("hermes_duration")
    index = row.get("hermes_evidence_index")
    candidate["hermes_duration"] = duration if duration in _DURATIONS else None
    candidate["hermes_evidence_index"] = (
        index if candidate["hermes_duration"] and type(index) is int and 0 <= index < len(articles) else None
    )
    return candidate


def get_performance(root, report_id=None):
    if report_id is not None and not valid_id(PERFORMANCE_ID, report_id):
        return absent("INVALID_REPORT_ID")
    try:
        repository = ReadOnlyResearchRepository(stock_database_path(root))
        records = repository.list_performance_records(report_id=report_id)
        return {"present": True, "records": to_json_value(records), "summary": summarize_performance(records)}
    except FileNotFoundError:
        return absent("DATABASE_MISSING")
    except DB_ERRORS:
        return absent("DATABASE_UNAVAILABLE")
