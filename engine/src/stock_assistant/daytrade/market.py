"""Normalize public observations without fabricating unavailable tape metrics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import json

from .contracts import KST, canonical, digest, identifier, instant, monotonic_value, number, symbol


@dataclass(frozen=True)
class QualityPolicy:
    # No trading freshness thresholds are silently chosen for the user.
    quote_max_age_seconds: int | None = None
    ranking_max_age_seconds: int | None = None
    candle_max_age_seconds: int | None = None

    def __post_init__(self):
        for v in (self.quote_max_age_seconds, self.ranking_max_age_seconds, self.candle_max_age_seconds):
            if v is not None and (type(v) is not int or v < 0):
                raise ValueError("invalid freshness threshold")


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    kind: str
    symbol: str
    market: str
    source: str
    source_timestamp: str | None
    received_at_utc: str
    received_monotonic: float
    session_id: str
    payload_json: str
    issues: tuple[str, ...]
    schema_version: str = "daytrade-market-v1"

    @property
    def data(self):
        return json.loads(self.payload_json)

    @property
    def entry_blocked(self):
        return bool(self.issues)

    def record(self):
        return dict(self.__dict__)

    @property
    def fingerprint(self):
        return digest(self.record())


def normalize(kind, payload, *, event_id, received_at, received_monotonic,
              session_id, quality, issuer="ALL", source=None):
    identifier(event_id)
    identifier(session_id)
    source = source or ("KRX_OPENAPI" if kind == "daily" else "TOSS_OPENAPI")
    identifier(source)
    received = instant(received_at)
    monotonic_value(received_monotonic)
    if not isinstance(payload, dict) or not isinstance(quality, QualityPolicy):
        raise ValueError("market payload and quality policy required")
    if issuer != "ALL":
        symbol(issuer)
    if kind != "rankings" and issuer == "ALL":
        raise ValueError("issuer required")
    issues = []

    def currency(row):
        if not isinstance(row, dict) or row.get("currency") != "KRW":
            raise ValueError("KRW required")

    def stamp(value, limit):
        if value is None:
            issues.append("SOURCE_TIMESTAMP_UNAVAILABLE")
            return None
        at = instant(value)
        if at > received:
            raise ValueError("future source observation")
        if limit is None:
            issues.append("FRESHNESS_POLICY_UNSET")
        elif (received - at).total_seconds() > limit:
            issues.append("STALE_" + kind.upper())
        return at.isoformat()

    source_at = None
    if kind == "rankings":
        rows = payload.get("rankings")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("bounded rankings array required")
        source_at = stamp(payload.get("rankedAt"), quality.ranking_max_age_seconds)
        data, seen = [], set()
        for row in rows:
            currency(row)
            code = symbol(row.get("symbol"))
            if code in seen:
                raise ValueError("duplicate ranking issuer")
            seen.add(code)
            data.append({"symbol": code, "trading_amount": number(row.get("tradingAmount")),
                         "trading_volume": number(row.get("tradingVolume"))})
        if not data:
            issues.append("EMPTY_RANKINGS")
        data = {"rankings": data}
    elif kind == "orderbook":
        currency(payload)
        source_at = stamp(payload.get("timestamp"), quality.quote_max_age_seconds)
        data = {}
        for side in ("asks", "bids"):
            levels = payload.get(side)
            if not isinstance(levels, list) or len(levels) > 100:
                raise ValueError("bounded orderbook array required")
            if any(not isinstance(row, dict) for row in levels):
                raise ValueError("orderbook level objects required")
            parsed = [{"price": number(row.get("price"), positive=True), "volume": number(row.get("volume"))} for row in levels]
            if len({r["price"] for r in parsed}) != len(parsed):
                raise ValueError("duplicate orderbook price level")
            data[side] = sorted((r for r in parsed if r["volume"] > 0), key=lambda r: r["price"], reverse=side == "bids")
        if not data["asks"] or not data["bids"]:
            issues.append("EMPTY_ORDERBOOK")
        elif data["bids"][0]["price"] >= data["asks"][0]["price"]:
            issues.append("CROSSED_ORDERBOOK")
    elif kind in {"trade", "price"}:
        currency(payload)
        source_at = stamp(payload.get("timestamp"), quality.quote_max_age_seconds)
        data = {"price": number(payload.get("price" if kind == "trade" else "lastPrice"), positive=True),
                "order_strength": "UNAVAILABLE", "tick_vwap": "UNAVAILABLE"}
        if kind == "trade":
            data["volume"] = number(payload.get("volume"))
    elif kind == "candles":
        rows = payload.get("candles")
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError("bounded candle array required")
        by_time = {}
        incomplete = 0
        for row in rows:
            currency(row)
            at = instant(row["timestamp"])
            if at.second or at.microsecond:
                raise ValueError("minute candle end must be minute-aligned")
            if at > received:
                incomplete += 1
                continue
            # Contract: 1m timestamps are END instants, not start instants.
            bar = {key: number(row.get(raw), positive=True) for key, raw in
                   (("open", "openPrice"), ("high", "highPrice"), ("low", "lowPrice"), ("close", "closePrice"))}
            if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > bar["high"]:
                raise ValueError("invalid OHLC range")
            bar.update(timestamp=at.isoformat(), volume=number(row.get("volume")))
            if at in by_time and by_time[at] != bar:
                raise ValueError("conflicting candle versions")
            by_time[at] = bar
        bars = [by_time[t] for t in sorted(by_time)]
        if bars:
            source_at = stamp(bars[-1]["timestamp"], quality.candle_max_age_seconds)
        else:
            issues.append("NO_COMPLETED_CANDLES")
        data = {"candles": bars, "incomplete_dropped": incomplete, "order_strength": "UNAVAILABLE", "tick_vwap": "UNAVAILABLE"}
    elif kind == "daily":
        currency(payload)
        from datetime import date
        day = date.fromisoformat(payload["trade_date"])
        if day >= received.astimezone(KST).date():
            raise ValueError("same-day or future KRX daily bar forbidden")
        data = {"trade_date": day.isoformat(), "close": number(payload.get("close"), positive=True),
                "volume": number(payload.get("volume"))}
    else:
        raise ValueError("unsupported market event")
    market = "KRX_DAILY" if kind == "daily" else "INTEGRATED_KR"
    return MarketEvent(event_id, kind, issuer, market, source, source_at, received.isoformat(),
                       received_monotonic, session_id, canonical(data), tuple(sorted(set(issues))))


def bar_typical_vwap_approx(event):
    if event.kind != "candles":
        raise ValueError("completed candle event required")
    bars = event.data["candles"]
    volume = sum((Decimal(b["volume"]) for b in bars), Decimal(0))
    if not volume:
        return {"name": "bar_typical_vwap_approx", "status": "UNAVAILABLE", "value": None}
    total = sum(((Decimal(b["high"]) + Decimal(b["low"]) + Decimal(b["close"])) / 3 * Decimal(b["volume"]) for b in bars), Decimal(0))
    return {"name": "bar_typical_vwap_approx", "status": "APPROXIMATION", "value": total / volume,
            "market": event.market}
