from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Market(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class AssetType(StrEnum):
    COMMON = "COMMON"
    ETF = "ETF"
    PREFERRED = "PREFERRED"
    SPAC = "SPAC"
    ETN = "ETN"
    LEVERAGED_ETF = "LEVERAGED_ETF"
    INVERSE_ETF = "INVERSE_ETF"
    OTHER = "OTHER"


class CompanyKind(StrEnum):
    GENERAL = "GENERAL"
    FINANCIAL = "FINANCIAL"
    FUND = "FUND"
    UNKNOWN = "UNKNOWN"


class CatalystStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PARTIAL = "PARTIAL"
    RUMOR_ONLY = "RUMOR_ONLY"
    EXPIRED = "EXPIRED"


class Decision(StrEnum):
    CANDIDATE = "CANDIDATE"
    BUY_HOLD = "BUY_HOLD"
    EXCLUDED = "EXCLUDED"


class ThesisStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    INVALIDATED = "INVALIDATED"
    CLOSED = "CLOSED"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Security:
    symbol: str
    name: str
    market: Market
    asset_type: AssetType
    company_kind: CompanyKind
    listed_on: date | None
    delisted_on: date | None = None

    def __post_init__(self) -> None:
        if not (self.symbol.isdigit() and len(self.symbol) == 6):
            raise ValueError("symbol must be a six-digit Korean security code")
        if not self.name.strip():
            raise ValueError("security name is required")
        if self.delisted_on is not None and self.listed_on is not None and self.delisted_on < self.listed_on:
            raise ValueError("delisted_on cannot precede listed_on")
        if self.asset_type is AssetType.ETF and self.company_kind is not CompanyKind.FUND:
            raise ValueError("ETF must use FUND company_kind")
        if self.company_kind is CompanyKind.FUND and self.asset_type is not AssetType.ETF:
            raise ValueError("FUND company_kind is reserved for ETF")


@dataclass(frozen=True, slots=True)
class OHLCV:
    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not (self.symbol.isdigit() and len(self.symbol) == 6):
            raise ValueError("OHLCV symbol must be six digits")
        if any(value < 0 for value in (self.open, self.high, self.low, self.close)):
            raise ValueError("OHLCV prices cannot be negative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below another price")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above another price")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if not self.source.strip():
            raise ValueError("OHLCV source is required")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class Evidence:
    source_type: str
    title: str
    url: str
    published_at: datetime
    observed_at: datetime
    official: bool
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_type.strip() or not self.title.strip() or not self.url.strip():
            raise ValueError("evidence source_type, title, and url are required")
        object.__setattr__(self, "published_at", _utc(self.published_at))
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.observed_at < self.published_at:
            raise ValueError("evidence cannot be observed before publication")


@dataclass(frozen=True, slots=True)
class FinancialSnapshot:
    symbol: str
    period_end: date
    published_at: datetime
    operating_income: Decimal
    operating_cash_flow: Decimal | None
    free_cash_flow: Decimal | None
    receivable_turnover: tuple[Decimal, ...] = ()
    inventory_turnover: tuple[Decimal, ...] = ()
    source_url: str = ""

    def __post_init__(self) -> None:
        if not (self.symbol.isdigit() and len(self.symbol) == 6):
            raise ValueError("financial symbol must be six digits")
        object.__setattr__(self, "published_at", _utc(self.published_at))
        if not self.source_url.strip():
            raise ValueError("financial source_url is required")


@dataclass(frozen=True, slots=True)
class FinancingEvent:
    symbol: str
    event_type: str
    announced_at: datetime
    dilutive: bool
    official: bool
    source_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "announced_at", _utc(self.announced_at))
        if not self.source_url.strip():
            raise ValueError("financing source_url is required")


@dataclass(frozen=True, slots=True)
class Catalyst:
    symbol: str
    category: str
    status: CatalystStatus
    announced_at: datetime
    valid_until: datetime | None
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "announced_at", _utc(self.announced_at))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _utc(self.valid_until))
            if self.valid_until < self.announced_at:
                raise ValueError("catalyst validity cannot end before announcement")
        if self.status is CatalystStatus.CONFIRMED and not any(item.official for item in self.evidence):
            raise ValueError("confirmed catalyst requires official evidence")


@dataclass(frozen=True, slots=True)
class ManagementRisk:
    symbol: str
    confirmed: bool
    category: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        if self.confirmed and not any(item.official for item in self.evidence):
            raise ValueError("confirmed management risk requires official evidence")


@dataclass(frozen=True, slots=True)
class Holding:
    broker: str
    symbol: str
    quantity: Decimal
    average_price: Decimal
    acquired_on: date | None = None

    def __post_init__(self) -> None:
        if not self.broker.strip():
            raise ValueError("broker is required")
        if not (self.symbol.isdigit() and len(self.symbol) == 6):
            raise ValueError("holding symbol must be six digits")
        if self.quantity <= 0 or self.average_price <= 0:
            raise ValueError("quantity and average_price must be positive")


@dataclass(frozen=True, slots=True)
class ThesisCard:
    thesis_id: str
    symbol: str
    created_at: datetime
    facts: tuple[str, ...]
    assumptions: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    additional_check_conditions: tuple[str, ...]
    status: ThesisStatus = ThesisStatus.DRAFT

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if not self.thesis_id.strip():
            raise ValueError("thesis_id is required")
        if not self.facts:
            raise ValueError("at least one confirmed fact is required")
        if not self.invalidation_conditions:
            raise ValueError("at least one invalidation condition is required")


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    symbol: str
    as_of: datetime
    decision: Decision
    score: Decimal
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    checks: tuple[str, ...]
    metrics: dict[str, Decimal | int | str | None] = field(default_factory=dict)
    name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _utc(self.as_of))
        if self.score < 0 or self.score > 100:
            raise ValueError("score must be between 0 and 100")


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [to_json_value(item) for item in value]
    if isinstance(value, list):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json_value(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: to_json_value(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value
