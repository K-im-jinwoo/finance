from __future__ import annotations

import json
import sys
import time
import urllib.error
from datetime import date
from typing import Callable

from .providers.http import NetworkError, ProviderError, get_json
from .providers.krx import KRX_DAILY_ENDPOINTS, KRX_SECURITY_ENDPOINTS
from .providers.toss import TOSS_BASE_URL


RETRY_DELAYS = (2, 5)
ENDPOINTS = {
    **{url: ("KRX_OPEN_API", market, "basDd") for market, url in KRX_DAILY_ENDPOINTS.items()},
    **{url: ("KRX_OPEN_API", market, "basDd") for market, url in KRX_SECURITY_ENDPOINTS.items()},
    TOSS_BASE_URL + "/api/v1/market-calendar/KR": ("TOSS_SECURITIES_OPEN_API", "KR", "date"),
}


def _emit_diagnostic(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), file=sys.stderr)


def _request_context(url: str, query: dict | None) -> dict:
    # Only the fixed public endpoints and validated dates can enter diagnostics.
    if url not in ENDPOINTS:
        raise ValueError("unsupported daily-refresh request endpoint")
    provider, market, date_key = ENDPOINTS[url]
    text = (query or {}).get(date_key)
    if not isinstance(text, str):
        raise ValueError("daily-refresh request requires a valid date")
    if date_key == "basDd":
        if len(text) != 8 or not text.isascii() or not text.isdigit():
            raise ValueError("daily-refresh request date must be YYYYMMDD")
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise ValueError("daily-refresh request requires a valid date") from None
    if day.isoformat() != text:
        raise ValueError("daily-refresh request requires an ISO date")
    return {"provider": provider, "endpoint": url, "market": market, "request_date": day.isoformat()}


def _is_timeout(error: NetworkError) -> bool:
    cause = error.__cause__
    if isinstance(cause, urllib.error.URLError):
        cause = cause.reason
    return isinstance(cause, TimeoutError)


class DailyRefreshRequests:
    """Retry individual daily GET timeouts; never repeat writes or token issuance."""

    def __init__(self, *, fetch_json: Callable = get_json, sleeper: Callable = time.sleep,
                 diagnostic: Callable = _emit_diagnostic):
        self.fetch_json = fetch_json
        self.sleeper = sleeper
        self.diagnostic = diagnostic

    def get_json(self, url: str, *, query: dict | None = None, **kwargs):
        context = _request_context(url, query)
        for attempt in range(1, len(RETRY_DELAYS) + 2):
            try:
                return self.fetch_json(url, query=query, **kwargs)
            except ProviderError as exc:
                retrying = isinstance(exc, NetworkError) and _is_timeout(exc) and attempt <= len(RETRY_DELAYS)
                transport = exc.request_context or {}
                exc.request_context = {
                    **context,
                    **{key: transport[key] for key in ("phase", "elapsed_seconds", "timeout_seconds", "http_status")
                       if key in transport},
                    "attempt": attempt, "max_attempts": len(RETRY_DELAYS) + 1,
                }
                event = {"stage": "DAILY_REFRESH", "event": "PROVIDER_REQUEST",
                         "status": "RETRYING" if retrying else "FAILED",
                         "error_class": type(exc).__name__, "request": exc.request_context}
                if retrying:
                    event["retry_after_seconds"] = RETRY_DELAYS[attempt - 1]
                self.diagnostic(event)
                if not retrying:
                    raise
                self.sleeper(RETRY_DELAYS[attempt - 1])
