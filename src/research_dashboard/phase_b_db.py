"""SQLite paths and saved-report reads shared by the Phase B bridges."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from decimal import InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qsl, urlsplit

from stock_assistant.research_data import APPLICATION_ID


UNIFIED_ID = re.compile(r"UR-[a-f0-9]{24}")
SWING_ID = re.compile(r"SW-[a-f0-9]{24}")
PERFORMANCE_ID = re.compile(r"(?:R-[0-9]{8}T[0-9]{4}Z-[A-F0-9]{8}|(?:UR|SW)-[a-f0-9]{24})")
REPORT_MODES = ("SYNTHETIC", "OBSERVED", "LIVE_RECORDED")
DB_ERRORS = (OSError, sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation, OverflowError)
_REPORT_FILTER = """
    FROM research_inputs
    WHERE kind=? AND (kind!='unified_report' OR symbol='ALL')
      AND julianday(observed_at)<=julianday(?)
      AND json_extract(payload_json,'$.contract')=?
"""
_SENSITIVE_QUERY = re.compile(r"token|secret|password|credential|api[_-]?key|auth|signature|crtfc_key", re.I)


def stock_database_path(root) -> Path:
    return _configured_path(root, "STOCK_DATABASE_PATH", "stock-assistant.sqlite3")


def research_database_path(root) -> Path:
    return _configured_path(root, "STOCK_UNIFIED_RESEARCH_DATABASE", "unified-research-inputs.sqlite3")


def _configured_path(root, variable, default_file):
    root = Path(root)
    value = os.environ.get(variable, "").strip()
    return root / value if value else root / ".runtime" / default_file


def absent(reason):
    return {"present": False, "reason": reason}


def valid_id(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def instant(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return timestamp


@contextmanager
def readonly_database(path):
    path = path.resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        yield db


@contextmanager
def _research_database(path):
    with readonly_database(path) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("invalid research database")
        yield db


def read_report_summaries(root, *, kind, contract, id_pattern):
    path = research_database_path(root)
    try:
        with _research_database(path) as db:
            rows = db.execute("""
                SELECT json_extract(payload_json,'$.report_id'), json_extract(payload_json,'$.as_of'),
                       observed_at, json_extract(payload_json,'$.policy_version'),
                       json_extract(payload_json,'$.status'), json_extract(payload_json,'$.expected_price_session'),
                       json_extract(payload_json,'$.price_session'), json_array_length(payload_json,'$.candidates'),
                       json_extract(payload_json,'$.decision_counts'), json_extract(payload_json,'$.mode')
                """ + _REPORT_FILTER + " ORDER BY julianday(observed_at) DESC, rowid DESC",
                (kind, datetime.now(timezone.utc).isoformat(), contract)).fetchall()
        reports = {}
        for identifier, as_of, observed_at, policy, status, expected, session, count, decisions, mode in rows:
            if not valid_id(id_pattern, identifier) or instant(as_of) > instant(observed_at):
                raise ValueError("invalid saved report")
            summary = {
                "report_id": identifier, "as_of": as_of, "observed_at": observed_at,
                "policy_version": policy, "status": status, "candidate_count": count or 0,
            }
            if mode in REPORT_MODES:
                summary["mode"] = mode
            if kind == "unified_report":
                summary["expected_price_session"] = expected
                summary["decision_counts"] = count_values(json.loads(decisions)) if decisions is not None else {}
            else:
                summary["price_session"] = session
            reports.setdefault(identifier, summary)
        items = list(reports.values())
        return {"present": True, "reports": items, "latest": items[0] if items else None}
    except FileNotFoundError:
        return absent("DATABASE_MISSING")
    except DB_ERRORS:
        return absent("DATABASE_UNAVAILABLE")


def read_report(root, report_id, *, kind, contract):
    path = research_database_path(root)
    try:
        with _research_database(path) as db:
            row = db.execute("SELECT payload_json, observed_at " + _REPORT_FILTER
                + " AND json_extract(payload_json,'$.report_id')=? ORDER BY julianday(observed_at) DESC, rowid DESC LIMIT 1",
                (kind, datetime.now(timezone.utc).isoformat(), contract, report_id)).fetchone()
        if row is None:
            return absent("REPORT_NOT_FOUND")
        report = json.loads(row[0])
        if instant(report["as_of"]) > instant(row[1]):
            raise ValueError("report precedes its analysis")
        return {"present": True, "report": report}
    except FileNotFoundError:
        return absent("DATABASE_MISSING")
    except DB_ERRORS:
        return absent("DATABASE_UNAVAILABLE")


def scalar_fields(value, fields):
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in fields.split()
        if key in value and (
            value[key] is None or type(value[key]) in (str, int, bool)
            or (type(value[key]) is float and math.isfinite(value[key]))
        )
    }


def string_list(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def dict_list(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def count_values(value):
    if not isinstance(value, dict):
        return {}
    return {key: count for key, count in value.items() if type(count) is int and count >= 0}


def safe_evidence(value):
    result = scalar_fields(value, "title category excerpt description published_at observed_at is_risk_signal")
    result["url"] = safe_url(value.get("url"))
    return result


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    if any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or any(_SENSITIVE_QUERY.search(key) for key, _ in parse_qsl(url.query))
                or _SENSITIVE_QUERY.search(url.fragment)):
            return None
    except ValueError:
        return None
    return value
