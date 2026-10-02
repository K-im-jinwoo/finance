from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from .models import IntradayCandle, MarketQuote, QuoteFreshness


@dataclass(frozen=True, slots=True)
class IntradaySignal:
    symbol: str
    observed_at: datetime
    freshness: QuoteFreshness
    age_seconds: int
    last_price: Decimal
    volume_ratio: Decimal | None
    volume_candle_at: datetime | None
    warnings: tuple[str, ...]


def quote_freshness(
    quote: MarketQuote,
    *,
    now: datetime,
    fresh_seconds: int = 300,
    delayed_seconds: int = 900,
) -> tuple[QuoteFreshness, int]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if fresh_seconds < 0 or delayed_seconds < fresh_seconds:
        raise ValueError("freshness thresholds are invalid")
    age = int((now.astimezone(timezone.utc) - quote.source_timestamp).total_seconds())
    if age < 0:
        raise ValueError("quote timestamp is in the future")
    if age <= fresh_seconds:
        return QuoteFreshness.FRESH, age
    if age <= delayed_seconds:
        return QuoteFreshness.DELAYED, age
    return QuoteFreshness.STALE, age


def build_intraday_signal(
    quote: MarketQuote,
    candles: list[IntradayCandle],
    *,
    now: datetime,
    volume_multiplier: Decimal = Decimal("3"),
) -> IntradaySignal:
    freshness, age_seconds = quote_freshness(quote, now=now)
    cutoff = now.astimezone(timezone.utc) - timedelta(minutes=1)
    closed = [item for item in candles if item.symbol == quote.symbol and item.timestamp <= cutoff]
    closed.sort(key=lambda item: item.timestamp)
    ratio: Decimal | None = None
    volume_candle_at: datetime | None = None
    warnings: list[str] = []
    if freshness is QuoteFreshness.DELAYED:
        warnings.append("QUOTE_DELAYED")
    elif freshness is QuoteFreshness.STALE:
        warnings.append("QUOTE_STALE")
    if len(closed) >= 21:
        latest = closed[-1]
        volume_candle_at = latest.timestamp
        baseline = Decimal(str(median(item.volume for item in closed[-21:-1])))
        if baseline > 0:
            ratio = Decimal(latest.volume) / baseline
            if ratio >= volume_multiplier:
                warnings.append("ONE_MINUTE_VOLUME_SPIKE")
    else:
        warnings.append("INSUFFICIENT_INTRADAY_HISTORY")
    return IntradaySignal(
        symbol=quote.symbol,
        observed_at=now.astimezone(timezone.utc),
        freshness=freshness,
        age_seconds=age_seconds,
        last_price=quote.last_price,
        volume_ratio=ratio,
        volume_candle_at=volume_candle_at,
        warnings=tuple(warnings),
    )
