"""Canonical serialization and explicit clocks shared by offline components."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re

KST = timezone(timedelta(hours=9))


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def number(value, *, positive=False):
    if isinstance(value, (bool, float)):
        raise ValueError("decimal string or integer required")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid decimal") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("finite non-negative decimal required")
    return result


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Z]{6}", value):
        raise ValueError("six-character Korean symbol required")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        raise ValueError("bounded identifier required")
    return value


def canonical(value):
    def encode(item):
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, datetime):
            return instant(item).isoformat()
        raise TypeError("non-canonical value")
    return json.dumps(value, default=encode, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def monotonic_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("finite monotonic time required")
    return value
