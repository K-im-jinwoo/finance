from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

KIND = {"GENERAL", "FINANCIAL", "ETF", "UNKNOWN"}

def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timezone is required")
    return result.astimezone(timezone.utc)

def number(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not result.is_finite():
        raise ValueError("finite number is required")
    return result

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def validate_dataset(data: dict) -> dict:
    """No backdating observations. Open execution quotes and closing evidence differ."""
    if data.get("schema_version") != 1 or data.get("mode") not in {"SYNTHETIC", "OBSERVED"}:
        raise ValueError("version 1 SYNTHETIC/OBSERVED dataset required")
    symbols = data.get("securities", {})
    for symbol, security in symbols.items():
        if not re.fullmatch(r"\d{6}", symbol) or security.get("kind") not in KIND:
            raise ValueError("invalid security contract")
    rows = data.get("events", [])
    seen = set()
    prior = None
    for event in rows:
        at = instant(event["at"])
        if prior is not None and at < prior:
            raise ValueError("events must be ordered")
        prior = at
        if not event.get("id") or event["id"] in seen:
            raise ValueError("duplicate or missing event id")
        seen.add(event["id"])
        if event["type"] not in {"REPORT", "CHECK", "OPEN", "MARK", "PAUSE", "RESUME", "END", "ERROR"}:
            raise ValueError("unknown event type")
        for key in ("available_at", "observed_at", "published_at"):
            if key in event and instant(event[key]) > at:
                raise ValueError(f"future {key}")
        if event["type"] in {"REPORT", "CHECK", "OPEN", "MARK"}:
            if not event.get("observed_at") or not event.get("source"):
                raise ValueError("observed_at and source required")
        if event["type"] == "REPORT":
            if not event.get("report_id") or not event.get("input_available_at"):
                raise ValueError("report lineage required")
            if instant(event["input_available_at"]) > at:
                raise ValueError("report uses future evidence")
            for candidate in event["candidates"]:
                if candidate["symbol"] not in symbols:
                    raise ValueError("candidate security is missing")
                if candidate["decision"] not in {"CANDIDATE", "BUY_HOLD", "EXCLUDED"}:
                    raise ValueError("invalid original decision")
        if event["type"] == "CHECK":
            if event["symbol"] not in symbols:
                raise ValueError("check security is missing")
            for field in ("price", "financial"):
                evidence = event.get(field)
                if evidence:
                    for key in ("published_at", "observed_at", "available_at"):
                        if not evidence.get(key) or instant(evidence[key]) > at:
                            raise ValueError(f"missing/future {field} {key}")
                    if not evidence.get("id") or not evidence.get("source"):
                        raise ValueError("evidence id and source required")
                    if instant(evidence["observed_at"]) < instant(evidence["published_at"]):
                        raise ValueError("observed before publication")
                    if instant(evidence["available_at"]) < max(instant(evidence["observed_at"]), instant(evidence["published_at"])):
                        raise ValueError("availability precedes observation/publication")
                    for value in evidence.get("closes", []):
                        if number(value) <= 0:
                            raise ValueError("positive close required")
                    for key in ("operating_income", "annual_operating_income", "ttm_operating_income", "ocf"):
                        if evidence.get(key) is not None:
                            number(evidence[key])
        if event["type"] in {"OPEN", "MARK"}:
            for symbol, value in event["prices"].items():
                if symbol not in symbols or number(value) <= 0:
                    raise ValueError("positive price and known symbol required")
            if event["type"] == "OPEN":
                if not event.get("session_id") or not event.get("calendar_source"):
                    raise ValueError("verified session metadata required")
                for key in ("open_at", "calendar_available_at"):
                    if not event.get(key) or instant(event[key]) > at:
                        raise ValueError("missing/future session metadata")
                if instant(event["observed_at"]) < instant(event["open_at"]):
                    raise ValueError("open quote observed before market opens")
    return {"dataset_id": digest(data), "mode": data["mode"], "event_count": len(rows),
            "supported_start": rows[0]["at"] if rows else None,
            "supported_end": rows[-1]["at"] if rows else None,
            "three_year_verified": False, "limitations": data.get("limitations", [])}

