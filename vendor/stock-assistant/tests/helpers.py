from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_assistant.models import OHLCV


UTC = timezone.utc


def make_bars(
    symbol: str = "005930",
    *,
    count: int = 80,
    start: date = date(2026, 1, 1),
    start_price: int = 100_000,
    step: int = 1_000,
    latest_volume_multiplier: int = 3,
) -> list[OHLCV]:
    bars: list[OHLCV] = []
    for index in range(count):
        close = Decimal(start_price + step * index)
        volume = 100_000 * (latest_volume_multiplier if index == count - 1 else 1)
        bars.append(
            OHLCV(
                symbol=symbol,
                trade_date=start + timedelta(days=index),
                open=close - Decimal("500"),
                high=close + Decimal("1000"),
                low=close - Decimal("1000"),
                close=close,
                volume=volume,
                source="TEST",
                observed_at=datetime.combine(start + timedelta(days=index), datetime.min.time(), UTC),
            )
        )
    return bars

