from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import OHLCV

SWING_POLICY = 'SWING_THEME_V1'
SWING_DECISION = 'SWING_CANDIDATE'
SWING_HORIZONS = (3, 5, 10)
SWING_COST_BPS = Decimal('30')


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
    scheduled_entry_date: date | None = None
    entry_tradability: str | None = None


def event_study(
    bars: list[OHLCV],
    *,
    signal_date: date,
    horizons: tuple[int, ...] = (5, 20, 60),
    round_trip_cost_bps: Decimal = Decimal("30"),
    market_sessions: tuple[date, ...] | None = None,
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
    by_date = {bar.trade_date: bar for bar in ordered}
    sessions = tuple(by_date) if market_sessions is None else market_sessions
    future = [day for day in sessions if day > signal_date]
    target = future[0] if future else None
    entry = by_date.get(target)
    tradability = None
    if market_sessions is not None:
        tradability = "UNCONFIRMED" if entry is None else (
            "TRADABLE" if entry.volume > 0 and entry.open > 0 else "NOT_TRADABLE")
    if entry is None or tradability == "NOT_TRADABLE":
        return EventStudyResult(
            symbol, signal_date, None, None,
            tuple(HorizonReturn(value, None, None, None) for value in horizons),
            target, tradability,
        )
    cost = round_trip_cost_bps / Decimal("10000")
    outcomes: list[HorizonReturn] = []
    for horizon in horizons:
        exit_bar = by_date.get(future[horizon]) if horizon < len(future) else None
        if exit_bar is None or (market_sessions is not None and (exit_bar.volume <= 0 or exit_bar.close <= 0)):
            outcomes.append(HorizonReturn(horizon, None, None, None))
            continue
        gross = (exit_bar.close / entry.open) - Decimal("1")
        outcomes.append(HorizonReturn(horizon, exit_bar.trade_date, gross, gross - cost))
    return EventStudyResult(symbol, signal_date, entry.trade_date, entry.open, tuple(outcomes),
                            target if market_sessions is not None else None, tradability)

