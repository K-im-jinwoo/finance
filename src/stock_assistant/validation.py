from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .models import OHLCV


class ContractError(ValueError):
    """Raised when an external payload violates a versioned contract."""


def parse_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise ContractError(f"{field} must be finite")
    return parsed


def parse_krx_ohlcv_rows(
    rows: Iterable[dict[str, Any]],
    *,
    observed_at: datetime,
    source: str = "KRX_OPEN_API",
) -> list[OHLCV]:
    required = {"symbol", "trade_date", "open", "high", "low", "close", "volume"}
    normalized: list[OHLCV] = []
    seen: set[tuple[str, date]] = set()
    aware_observed_at = observed_at.astimezone(timezone.utc)

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ContractError(f"row {index} must be an object")
        missing = required - row.keys()
        if missing:
            raise ContractError(f"row {index} missing fields: {sorted(missing)}")
        try:
            trade_date = date.fromisoformat(str(row["trade_date"]))
            volume_decimal = parse_decimal(row["volume"], "volume")
            if volume_decimal != volume_decimal.to_integral_value():
                raise ContractError("volume must be an integer")
            point = OHLCV(
                symbol=str(row["symbol"]),
                trade_date=trade_date,
                open=parse_decimal(row["open"], "open"),
                high=parse_decimal(row["high"], "high"),
                low=parse_decimal(row["low"], "low"),
                close=parse_decimal(row["close"], "close"),
                volume=int(volume_decimal),
                source=source,
                observed_at=aware_observed_at,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError(f"row {index}: {exc}") from exc
        key = (point.symbol, point.trade_date)
        if key in seen:
            raise ContractError(f"duplicate OHLCV row: {point.symbol} {point.trade_date}")
        seen.add(key)
        normalized.append(point)

    return sorted(normalized, key=lambda item: (item.symbol, item.trade_date))


def assert_point_in_time(
    as_of: datetime,
    *,
    observed_at: datetime | None = None,
    published_at: datetime | None = None,
    label: str = "record",
) -> None:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    cutoff = as_of.astimezone(timezone.utc)
    for name, value in (("observed_at", observed_at), ("published_at", published_at)):
        if value is not None and value.astimezone(timezone.utc) > cutoff:
            raise ContractError(f"{label} {name} is later than as_of")


def validate_analysis_time(
    as_of: datetime,
    *,
    now: datetime | None = None,
    label: str = "as_of",
) -> datetime:
    if as_of.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    normalized = as_of.astimezone(timezone.utc)
    if normalized > current.astimezone(timezone.utc):
        raise ValueError(f"{label} cannot be in the future")
    return normalized
