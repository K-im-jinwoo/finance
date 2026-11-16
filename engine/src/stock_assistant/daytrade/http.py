"""Injected public REST transport, per-group cooldowns, and bounded diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import math
import re

from .contracts import monotonic_value


class RateGroup(StrEnum):
    RANKINGS = "RANKINGS"  # Internal name; the provider calls this group RANKING.
    MARKET_DATA = "MARKET_DATA"
    MARKET_DATA_CHART = "MARKET_DATA_CHART"


PATH_GROUP = {"/api/v1/rankings": RateGroup.RANKINGS, "/api/v1/prices": RateGroup.MARKET_DATA,
              "/api/v1/orderbook": RateGroup.MARKET_DATA, "/api/v1/trades": RateGroup.MARKET_DATA,
              "/api/v1/candles": RateGroup.MARKET_DATA_CHART}


def safe_error(payload, headers, *, secrets=()):
    """Expose only ApiError.code and X-Request-Id; never message/data/body IDs."""
    def safe(value, pattern):
        return (isinstance(value, str) and re.fullmatch(pattern, value) is not None
                and not any(s and s in value for s in secrets))
    if isinstance(payload, bytes):
        if len(payload) > 65536:
            payload = None
        else:
            try:
                payload = json.loads(payload)
            except (ValueError, UnicodeError):
                payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    result = {}
    if safe(code, r"[a-z][a-z0-9-]{0,63}"):
        result["api_error_code"] = code
    request_id = next((v for k, v in headers.items() if k.lower() == "x-request-id"), None)
    if safe(request_id, r"[A-Za-z0-9-]{8,80}"):
        result["request_id"] = request_id
    return result


@dataclass(frozen=True)
class HttpResult:
    status: int
    payload: object
    headers: dict


@dataclass
class GroupState:
    consecutive_429: int = 0
    next_allowed: float = 0
    degraded: bool = False


class RestAdapter:
    """One attempt per fetch, no sleeping or implicit network implementation.

    The scheduler must call again after retry_at. A successful response does not
    clear a latched degraded state; explicit recovery is required per group.
    """
    def __init__(self, transport, *, rng, base_delay=1, maximum_delay=60, degrade_after=3):
        if (isinstance(base_delay, bool) or not math.isfinite(base_delay) or base_delay <= 0
                or maximum_delay < base_delay or not math.isfinite(maximum_delay)
                or type(degrade_after) is not int or degrade_after < 1):
            raise ValueError("invalid backoff configuration")
        self.transport, self.rng = transport, rng
        self.base, self.maximum, self.threshold = base_delay, maximum_delay, degrade_after
        self.groups = {g: GroupState() for g in RateGroup}

    @property
    def entry_blocked(self):
        return any(s.degraded or s.consecutive_429 for s in self.groups.values())

    def fetch(self, path, query, *, now, purpose="OBSERVE"):
        monotonic_value(now)
        if path not in PATH_GROUP or purpose not in {"OBSERVE", "EXIT_MONITOR"}:
            raise ValueError("public read-only scope required")
        group, state = PATH_GROUP[path], self.groups[PATH_GROUP[path]]
        if state.degraded and purpose != "EXIT_MONITOR":
            return {"status": "RATE_LIMIT_DEGRADED", "group": group, "entry_blocked": True}
        if now < state.next_allowed:
            return {"status": "BACKOFF", "group": group, "retry_at": state.next_allowed,
                    "entry_blocked": self.entry_blocked}
        # Transport is intentionally injected; tests never construct a real client.
        try:
            response = self.transport(path, dict(query))
        except Exception:
            return {"status": "TRANSPORT_ERROR", "group": group, "entry_blocked": True}
        if type(response.status) is not int:
            raise ValueError("invalid HTTP status")
        if response.status == 429:
            state.consecutive_429 += 1
            base = min(self.maximum, self.base * 2 ** min(state.consecutive_429 - 1, 30))
            delay = min(self.maximum, base + self.rng.uniform(0, base * .25))
            retry = next((v for k, v in response.headers.items() if k.lower() == "retry-after"), None)
            if isinstance(retry, str) and re.fullmatch(r"[0-9]{1,6}", retry):
                delay = max(delay, int(retry))
            state.next_allowed = now + delay
            state.degraded |= state.consecutive_429 >= self.threshold
            return {"status": "RATE_LIMIT_DEGRADED" if state.degraded else "BACKOFF", "group": group,
                    "retry_at": state.next_allowed, "entry_blocked": self.entry_blocked,
                    **safe_error(response.payload, response.headers)}
        if response.status != 200:
            state.consecutive_429 = 0
            return {"status": "HTTP_ERROR", "group": group, "http_status": response.status,
                    "entry_blocked": True, **safe_error(response.payload, response.headers)}
        state.consecutive_429 = 0
        return {"status": "OK", "group": group, "payload": response.payload,
                "entry_blocked": self.entry_blocked}

    def recover(self, group, *, verified=False):
        state = self.groups[RateGroup(group)]
        if not verified or state.consecutive_429:
            raise ValueError("successful exit-monitor observation and verified recovery required")
        state.degraded = False
