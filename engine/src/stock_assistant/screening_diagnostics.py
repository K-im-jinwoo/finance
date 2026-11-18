"""Minimum-field report audit. No holdings, credentials, migration or provider calls."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import re

from .research_repository import ReadOnlyResearchRepository
from .validation import validate_analysis_time


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_EXCLUSIONS = {"OPERATING_LOSS", "OCF_NON_POSITIVE", "MANAGEMENT_RISK_CONFIRMED", "UNSUPPORTED_ASSET_TYPE"}
_DATA = {"DATA_UNAVAILABLE", "PROFIT_PERIODS_UNAVAILABLE", "TTM_STALE", "OCF_UNAVAILABLE",
         "DILUTION_HISTORY_UNAVAILABLE", "MANAGEMENT_HISTORY_UNAVAILABLE", "CATALYST_HISTORY_UNAVAILABLE",
         "COMPANY_KIND_UNAVAILABLE", "ETF_METRICS_UNAVAILABLE", "ETF_DUE_DILIGENCE_INCOMPLETE",
         "FINANCIAL_METRICS_UNAVAILABLE", "FINANCIAL_METRICS_INCOMPLETE"}
_PRICE = {"LIQUIDITY_LOW"}
_REVIEW = {"FCF_NEGATIVE_REVIEW", "TURNOVER_WEAKENING", "DILUTION_EVENT", "DILUTION_SCALE_UNAVAILABLE",
           "REFIXING_PRESENT", "REPEATED_DILUTION", "MANAGEMENT_RISK_OFFICIAL_REVIEW", "RUMOR_ONLY",
           "FINANCIAL_SPECIALIST_REVIEW_REQUIRED"}


def _codes(raw) -> list[str]:
    import json
    values = json.loads(raw) if raw is not None else []
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("stored report warnings must be a bounded array")
    return [value if isinstance(value, str) and _CODE.fullmatch(value) else "UNRECOGNIZED_WARNING"
            for value in values]


def audit_screening(repository: ReadOnlyResearchRepository, *, as_of: datetime, report_limit: int = 8) -> dict:
    as_of = validate_analysis_time(as_of)
    if isinstance(report_limit, bool) or not isinstance(report_limit, int) or not 1 <= report_limit <= 20:
        raise ValueError("report_limit must be between 1 and 20")
    counts, reasons, groups = Counter(), Counter(), Counter()
    reports = []
    latest_candidates = []
    news_count = 0
    with repository._connect() as connection:
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "reports" not in table_names:
            return {"status": "UNAVAILABLE", "reason": "REPORT_TABLE_MISSING", "read_only": True}
        selected = connection.execute(
            "SELECT report_id,as_of FROM reports WHERE julianday(as_of)<=julianday(?) "
            "ORDER BY julianday(as_of) DESC,report_id DESC LIMIT ?",
            (as_of.isoformat(), report_limit),
        ).fetchall()
        for report_index, report in enumerate(selected):
            rows = connection.execute(
                "SELECT json_extract(c.value,'$.symbol') AS symbol, json_extract(c.value,'$.name') AS name, "
                "json_extract(c.value,'$.decision') AS decision, json_extract(c.value,'$.review_tier') AS tier, "
                "json_extract(c.value,'$.score') AS score, json_extract(c.value,'$.warnings') AS warnings, "
                "json_array_length(json_extract(c.value,'$.checks')) AS checks_count "
                "FROM reports r,json_each(r.payload_json,'$.candidates') c WHERE r.report_id=? "
                "ORDER BY CAST(c.key AS INTEGER) LIMIT 101", (report["report_id"],),
            ).fetchall()
            if len(rows) > 100:
                raise ValueError("stored report exceeds the audit candidate limit")
            report_counts = Counter()
            for row in rows:
                decision = row["decision"] if row["decision"] in {"CANDIDATE", "BUY_HOLD", "EXCLUDED"} else "UNKNOWN"
                counts[decision] += 1
                report_counts[decision] += 1
                warnings = list(dict.fromkeys(_codes(row["warnings"])))
                categories = set()
                for code in warnings:
                    reasons[code] += 1
                    categories.add("HARD_EXCLUSION" if code in _EXCLUSIONS and decision == "EXCLUDED"
                                   else "CAUSE_REVIEW" if code in _EXCLUSIONS else "DATA_INCOMPLETE" if code in _DATA
                                   else "PRICE_OR_LIQUIDITY" if code in _PRICE else "CAUSE_REVIEW" if code in _REVIEW
                                   else "OTHER_WARNING")
                if decision == "BUY_HOLD" and row["checks_count"] and not warnings:
                    categories.add("CHECKS_WITHOUT_WARNING")
                for category in categories:
                    groups[category] += 1
                if report_index == 0:
                    latest_candidates.append({"symbol": row["symbol"], "name": row["name"], "decision": decision,
                                              "review_tier": row["tier"], "score": row["score"],
                                              "warnings": warnings, "checks_count": row["checks_count"] or 0,
                                              "diagnostic_groups": sorted(categories)})
            news = connection.execute(
                "SELECT json_extract(n.value,'$.status') FROM reports r, "
                "json_each(r.payload_json,'$.news_discovery.candidates') n WHERE r.report_id=? LIMIT 101",
                (report["report_id"],),
            ).fetchall()
            if len(news) > 100:
                raise ValueError("stored report exceeds the audit news limit")
            news_count += sum(row[0] != "EXCLUDED" for row in news)
            reports.append({"report_id": report["report_id"], "as_of": report["as_of"],
                            "decision_counts": dict(report_counts), "news_candidates": len(news)})
        coverage = {}
        for table, field in (("ohlcv", "trade_date"), ("financial_snapshots", "published_at")):
            if table in table_names:
                # Bound observation/publication to this audit's cutoff; do not count future evidence.
                available_field = "observed_at" if table == "ohlcv" else "published_at"
                row = connection.execute(
                    f"SELECT COUNT(DISTINCT symbol),MAX({field}) FROM {table} WHERE julianday({available_field})<=julianday(?)",
                    (as_of.isoformat(),),
                ).fetchone()
                coverage[table] = {"symbols": row[0], "latest_available_date": row[1]}
        if "securities" in table_names:
            row = connection.execute("SELECT COUNT(*),SUM(json_extract(payload_json,'$.asset_type')='COMMON' "
                                     "AND json_extract(payload_json,'$.company_kind')='GENERAL') FROM securities").fetchone()
            coverage["securities"] = {"total": row[0], "general_common": row[1] or 0}
        if "financial_snapshots" in table_names:
            row = connection.execute(
                "WITH ranked AS (SELECT payload_json,ROW_NUMBER() OVER(PARTITION BY symbol "
                "ORDER BY julianday(published_at) DESC,period_end DESC) AS rank FROM financial_snapshots "
                "WHERE julianday(published_at)<=julianday(?)) SELECT COUNT(*),"
                "SUM(json_extract(payload_json,'$.annual_operating_income') IS NOT NULL),"
                "SUM(json_extract(payload_json,'$.ttm_operating_income') IS NOT NULL),"
                "SUM(json_extract(payload_json,'$.operating_cash_flow') IS NOT NULL),"
                "SUM(json_array_length(json_extract(payload_json,'$.receivable_turnover'))>=2 AND "
                "json_array_length(json_extract(payload_json,'$.inventory_turnover'))>=2) "
                "FROM ranked WHERE rank=1", (as_of.isoformat(),),
            ).fetchone()
            coverage["financial_fields"] = dict(zip(
                ("latest_symbols", "annual_profit_present", "ttm_profit_present", "ocf_present", "turnover_history_present"),
                (value or 0 for value in row)))
    return {"status": "OK" if reports else "NO_REPORTS", "as_of": as_of.isoformat(), "read_only": True,
            "reports_examined": len(reports), "candidate_observations": sum(counts.values()),
            "decision_counts": dict(sorted(counts.items())), "warning_counts": dict(sorted(reasons.items())),
            "diagnostic_group_counts": dict(sorted(groups.items())), "reports": reports,
            "latest_candidates": latest_candidates, "data_coverage": coverage,
            "news_fixed_hold_label_observations": news_count,
            "limitations": ["Diagnostic categories do not change saved decisions",
                            "Publication dates alone do not certify historical financial availability",
                            "The current news renderer uses a fixed hold label for non-excluded news leads"]}

