from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import fmean

from .models import OHLCV


class InsufficientHistory(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PriceFeatures:
    close: Decimal
    sma20: Decimal
    sma60: Decimal
    return5: Decimal
    return20: Decimal
    drawdown60: Decimal
    volume_ratio20: Decimal
    average_value20: Decimal
    atr14: Decimal


def _ordered_unique(bars: list[OHLCV]) -> list[OHLCV]:
    ordered = sorted(bars, key=lambda item: item.trade_date)
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise ValueError("duplicate trade_date in price history")
    if len({item.symbol for item in ordered}) > 1:
        raise ValueError("price history contains multiple symbols")
    return ordered


def simple_average(values: list[Decimal]) -> Decimal:
    if not values:
        raise InsufficientHistory("no values")
    return sum(values, Decimal("0")) / Decimal(len(values))


def wilder_atr(bars: list[OHLCV], period: int = 14) -> Decimal:
    ordered = _ordered_unique(bars)
    if period < 1:
        raise ValueError("period must be positive")
    if len(ordered) < period + 1:
        raise InsufficientHistory(f"ATR{period} requires at least {period + 1} bars")
    true_ranges: list[Decimal] = []
    for previous, current in zip(ordered, ordered[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr = simple_average(true_ranges[:period])
    for current_range in true_ranges[period:]:
        atr = (atr * Decimal(period - 1) + current_range) / Decimal(period)
    return atr


def price_features(bars: list[OHLCV]) -> PriceFeatures:
    ordered = _ordered_unique(bars)
    if len(ordered) < 61:
        raise InsufficientHistory("price features require at least 61 trading days")
    closes = [item.close for item in ordered]
    volumes = [Decimal(item.volume) for item in ordered]
    latest = ordered[-1]
    previous_twenty_volumes = volumes[-21:-1]
    avg_volume = simple_average(previous_twenty_volumes)
    volume_ratio = Decimal("0") if avg_volume == 0 else volumes[-1] / avg_volume
    high60 = max(closes[-60:])
    average_value20 = simple_average(
        [bar.close * Decimal(bar.volume) for bar in ordered[-20:]]
    )
    return PriceFeatures(
        close=latest.close,
        sma20=simple_average(closes[-20:]),
        sma60=simple_average(closes[-60:]),
        return5=(latest.close / closes[-6]) - Decimal("1"),
        return20=(latest.close / closes[-21]) - Decimal("1"),
        drawdown60=(latest.close / high60) - Decimal("1"),
        volume_ratio20=volume_ratio,
        average_value20=average_value20,
        atr14=wilder_atr(ordered[-61:], 14),
    )


def trend_is_stable_or_rising(values: tuple[Decimal, ...], tolerance: Decimal = Decimal("0.05")) -> bool | None:
    if len(values) < 2:
        return None
    baseline = values[0]
    if baseline == 0:
        return values[-1] >= 0
    return values[-1] >= baseline * (Decimal("1") - tolerance)

