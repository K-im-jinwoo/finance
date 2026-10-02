from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import OHLCV


@dataclass(frozen=True, slots=True)
class HorizonReturn:
    trading_days: int
    exit_date: date | None
    gross_return: Decimal | None
    net_return: Decimal | None


@dataclass(frozen=True, slots=True)
class EventStudyResult:
    symbol: str
    signal_date: date
    entry_date: date | None
    entry_price: Decimal | None
    horizons: tuple[HorizonReturn, ...]


def event_study(
    bars: list[OHLCV],
    *,
    signal_date: date,
    horizons: tuple[int, ...] = (5, 20, 60),
    round_trip_cost_bps: Decimal = Decimal("30"),
) -> EventStudyResult:
    if not horizons or any(value < 1 for value in horizons):
        raise ValueError("horizons must contain positive trading-day counts")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")
    ordered = sorted(bars, key=lambda item: item.trade_date)
    if len({item.trade_date for item in ordered}) != len(ordered):
        raise ValueError("duplicate trade_date in event-study input")
    symbols = {item.symbol for item in ordered}
    if len(symbols) > 1:
        raise ValueError("event-study input contains multiple symbols")
    symbol = next(iter(symbols), "")
    entry_index = next(
        (index for index, item in enumerate(ordered) if item.trade_date > signal_date),
        None,
    )
    if entry_index is None:
        return EventStudyResult(
            symbol, signal_date, None, None,
            tuple(HorizonReturn(value, None, None, None) for value in horizons),
        )
    entry = ordered[entry_index]
    cost = round_trip_cost_bps / Decimal("10000")
    outcomes: list[HorizonReturn] = []
    for horizon in horizons:
        exit_index = entry_index + horizon
        if exit_index >= len(ordered):
            outcomes.append(HorizonReturn(horizon, None, None, None))
            continue
        exit_bar = ordered[exit_index]
        gross = (exit_bar.close / entry.open) - Decimal("1")
        outcomes.append(HorizonReturn(horizon, exit_bar.trade_date, gross, gross - cost))
    return EventStudyResult(symbol, signal_date, entry.trade_date, entry.open, tuple(outcomes))

