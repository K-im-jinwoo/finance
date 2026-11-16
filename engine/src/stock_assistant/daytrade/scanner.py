"""Pure integrated-market watch selection. Selection never authorizes execution.

Supply normalized rankings MarketEvents, or REST result payloads with an explicit
scope='INTEGRATED_KR'. Raw ranking price/name fields are retained when available;
normalized rankings can instead use a fresh orderbook for the minimum-price test.
Thresholds are caller-supplied assumptions, not approved trading policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .contracts import KST, instant, number, symbol
from .market import MarketEvent, QualityPolicy, normalize

SCOPE = "INTEGRATED_KR"
RISK_FLAGS = ("halted", "delisting", "warning_or_caution", "major_negative_disclosure")


@dataclass(frozen=True, kw_only=True)
class ScannerConfig:
    min_price: str | Decimal
    min_turnover: str | Decimal
    max_spread_bps: str | Decimal
    quote_max_age_seconds: int
    ranking_max_age_seconds: int
    max_watch: int = 10

    def __post_init__(self):
        for key in ("min_price", "min_turnover", "max_spread_bps"):
            object.__setattr__(self, key, number(getattr(self, key), positive=key != "min_turnover"))
        for value in (self.quote_max_age_seconds, self.ranking_max_age_seconds):
            if type(value) is not int or value < 0:
                raise ValueError("explicit non-negative freshness seconds required")
        if type(self.max_watch) is not int or not 1 <= self.max_watch <= 100:
            raise ValueError("max_watch must be between 1 and 100")


@dataclass(frozen=True)
class ScanResult:
    selected: list[dict]
    rejected: list[dict]


def risk_reasons(metadata):
    """Missing metadata is unknown; only explicit adverse flags imply exclusion."""
    if metadata is None:
        return ()
    if not isinstance(metadata, dict):
        raise ValueError("risk metadata must be an object")
    reasons = []
    for flag in RISK_FLAGS:
        if flag in metadata and type(metadata[flag]) is not bool:
            raise ValueError("risk flags must be booleans")
        if metadata.get(flag) is True:
            reasons.append(flag.upper())
    return tuple(reasons)


def event_quality_issues(event, now, max_age_seconds):
    """Recheck age at decision time; a clean receive-time flag is insufficient."""
    at = instant(now)
    reasons = list(event.issues)
    received = instant(event.received_at_utc)
    if received > at:
        reasons.append("FUTURE_OBSERVATION")
    if received.astimezone(KST).date() != at.astimezone(KST).date():
        reasons.append("SESSION_DATE_MISMATCH")
    if event.source_timestamp is None:
        reasons.append("SOURCE_TIMESTAMP_UNAVAILABLE")
    else:
        source_at = instant(event.source_timestamp)
        age = (at - source_at).total_seconds()
        if source_at.astimezone(KST).date() != at.astimezone(KST).date():
            reasons.append("SESSION_DATE_MISMATCH")
        if age < 0:
            reasons.append("FUTURE_SOURCE_TIMESTAMP")
        elif age > max_age_seconds:
            reasons.append("STALE_" + event.kind.upper())
    return tuple(sorted(set(reasons)))


def orderbook_metrics(event, *, now, max_age_seconds, issuer):
    """Shared executable-side references; no invented quote for missing depth."""
    if event is None:
        return {"issues": ("ORDERBOOK_UNAVAILABLE",), "bid": None, "ask": None,
                "bid_volume": None, "spread_bps": None}
    if not isinstance(event, MarketEvent) or event.kind != "orderbook" or event.symbol != issuer:
        raise ValueError("matching normalized orderbook required")
    reasons = list(event_quality_issues(event, now, max_age_seconds))
    if event.market != SCOPE:
        reasons.append("SCOPE_MISMATCH")
    data = event.data
    bid = ask = bid_volume = spread = None
    if not data["bids"] or not data["asks"]:
        reasons.append("EMPTY_ORDERBOOK")
    else:
        bid = number(data["bids"][0]["price"], positive=True)
        ask = number(data["asks"][0]["price"], positive=True)
        bid_volume = number(data["bids"][0]["volume"])
        if bid >= ask:
            reasons.append("CROSSED_ORDERBOOK")
        else:
            # Midpoint denominator, consistently used by scanner and strategy.
            spread = (ask - bid) * 20000 / (ask + bid)
    return {"issues": tuple(sorted(set(reasons))), "bid": bid, "ask": ask,
            "bid_volume": bid_volume, "spread_bps": spread}


def _snapshot(snapshot, *, config, now, label):
    raw_rows = None
    if isinstance(snapshot, MarketEvent):
        event = snapshot
        if event.kind != "rankings" or event.market != SCOPE:
            raise ValueError("integrated-market rankings required")
    else:
        if not isinstance(snapshot, dict) or snapshot.get("scope") != SCOPE or "error" in snapshot:
            raise ValueError("explicit INTEGRATED_KR ranking scope required")
        payload = snapshot.get("result", snapshot)
        event = normalize("rankings", payload, event_id="scanner-" + label,
            received_at=now, received_monotonic=0, session_id="scanner",
            quality=QualityPolicy(ranking_max_age_seconds=config.ranking_max_age_seconds))
        raw_rows = {row["symbol"]: row for row in payload["rankings"]}
    rows, ranks = {}, set()
    for index, row in enumerate(event.data["rankings"], 1):
        extra = raw_rows[row["symbol"]] if raw_rows is not None else {}
        rank = extra.get("rank", index)
        if type(rank) is not int or not 1 <= rank <= 100 or rank in ranks:
            raise ValueError("unique ranking positions between 1 and 100 required")
        ranks.add(rank)
        raw_price = extra.get("price")
        if raw_price is not None and (not isinstance(raw_price, dict) or "lastPrice" not in raw_price):
            raise ValueError("ranking price.lastPrice required")
        price = number(raw_price["lastPrice"], positive=True) if raw_price is not None else None
        name = extra.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("ranking name must be text")
        rows[row["symbol"]] = {**row, "rank": rank, "price": price, "name": name}
    return event, rows


def scan_rankings(turnover_snapshot, volume_snapshot=None, *, config, now,
                  risk_meta=None, quotes=None):
    """Union two Top-100 lists, then sort by turnover, rank and symbol.

    A missing optional quote permits watch discovery only (entry_blocked=True).
    A supplied but invalid quote rejects the candidate. Missing risk metadata is
    explicitly reported as unknown and never described as a completed risk check.
    """
    if not isinstance(config, ScannerConfig):
        raise ValueError("ScannerConfig required")
    at = instant(now)
    risk_meta = {} if risk_meta is None else risk_meta
    quotes = {} if quotes is None else quotes
    if not isinstance(risk_meta, dict) or not isinstance(quotes, dict):
        raise ValueError("symbol-keyed risk metadata and quote mappings required")
    first, turnover = _snapshot(turnover_snapshot, config=config, now=at, label="turnover")
    second, volume = (None, {}) if volume_snapshot is None else _snapshot(
        volume_snapshot, config=config, now=at, label="volume")
    events = [e for e in (first, second) if e is not None]
    snapshot_issues = sorted({issue for e in events for issue in
        event_quality_issues(e, at, config.ranking_max_age_seconds)})
    if len({e.session_id for e in events}) > 1:
        snapshot_issues.append("SESSION_ID_MISMATCH")
    valid_until = min((instant(e.source_timestamp) + timedelta(seconds=config.ranking_max_age_seconds)
                       for e in events if e.source_timestamp), default=None)
    selected, rejected = [], []
    for code in sorted(turnover.keys() | volume.keys()):
        # Use the newest source snapshot for accumulated amount and price.
        available = [(first, turnover[code])] if code in turnover else []
        if code in volume:
            available.append((second, volume[code]))
        _, row = max(available, key=lambda pair: instant(pair[0].source_timestamp)
                     if pair[0].source_timestamp else instant(pair[0].received_at_utc))
        metadata = risk_meta.get(code)
        reasons = snapshot_issues + list(risk_reasons(metadata))
        book = orderbook_metrics(quotes.get(code), now=at,
                                max_age_seconds=config.quote_max_age_seconds, issuer=code)
        if quotes.get(code) is not None:
            reasons.extend(book["issues"])
        price = book["bid"] if not book["issues"] else row["price"]
        if price is None:
            reasons.append("PRICE_UNAVAILABLE")
        elif price < config.min_price:
            reasons.append("LOW_PRICE")
        amount = number(row["trading_amount"])
        if amount < config.min_turnover:
            reasons.append("LOW_TURNOVER")
        spread = book["spread_bps"]
        if spread is not None and spread > config.max_spread_bps:
            reasons.append("WIDE_SPREAD")
        if reasons:
            rejected.append({"symbol": code, "reason": sorted(set(reasons))[0],
                             "reasons": sorted(set(reasons))})
            continue
        selection_reasons = ["TURNOVER_ELIGIBLE"]
        if code in turnover:
            selection_reasons.append("TURNOVER_RANKED")
        if code in volume:
            selection_reasons.append("VOLUME_RANKED")
        selection_reasons.append("QUOTE_ELIGIBLE" if not book["issues"] else "QUOTE_UNAVAILABLE_WATCH_ONLY")
        selection_reasons.append("RISK_METADATA_PROVIDED" if metadata is not None else "RISK_METADATA_UNAVAILABLE")
        selected.append({"symbol": code, "name": row["name"] or (metadata or {}).get("name") or code,
            "rank_turnover": turnover.get(code, {}).get("rank"),
            "rank_volume": volume.get(code, {}).get("rank"), "turnover": amount,
            "spread_bps": spread, "selection_reasons": selection_reasons, "scope": SCOPE,
            "entry_blocked": bool(book["issues"]),
            "valid_until": valid_until.isoformat() if valid_until is not None else None})
    selected.sort(key=lambda r: (-r["turnover"], r["rank_turnover"] or 101,
                                  r["rank_volume"] or 101, r["symbol"]))
    rejected.extend({"symbol": row["symbol"], "reason": "WATCH_LIMIT", "reasons": ["WATCH_LIMIT"]}
                    for row in selected[config.max_watch:])
    return ScanResult(selected[:config.max_watch], sorted(rejected, key=lambda r: r["symbol"]))
