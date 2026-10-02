from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from .identifiers import require_korean_security_symbol


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


class ReviewTier(StrEnum):
    PRIORITY_REVIEW = "PRIORITY_REVIEW"
    STANDARD_REVIEW = "STANDARD_REVIEW"
    EXCLUDED = "EXCLUDED"


class StrategyType(StrEnum):
    BOTTOM_REBOUND = "BOTTOM_REBOUND"
    MOMENTUM_CONTINUATION = "MOMENTUM_CONTINUATION"
    HYBRID = "HYBRID"
    WAIT_FOR_SETUP = "WAIT_FOR_SETUP"
    UNAVAILABLE = "UNAVAILABLE"


class HoldingPeriod(StrEnum):
    SEVERAL_DAYS = "SEVERAL_DAYS"
    SEVERAL_WEEKS = "SEVERAL_WEEKS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ThesisStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    INVALIDATED = "INVALIDATED"
    CLOSED = "CLOSED"


class QuoteFreshness(StrEnum):
    FRESH = "FRESH"
    DELAYED = "DELAYED"
    STALE = "STALE"


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
        require_korean_security_symbol(self.symbol)
        if not self.name.strip():
            raise ValueError("security name is required")
        if self.delisted_on is not None and self.listed_on is not None and self.delisted_on < self.listed_on:
            raise ValueError("delisted_on cannot precede listed_on")
        etf_types = {AssetType.ETF, AssetType.LEVERAGED_ETF, AssetType.INVERSE_ETF}
        if self.asset_type in etf_types and self.company_kind is not CompanyKind.FUND:
            raise ValueError("ETF types must use FUND company_kind")
        if self.company_kind is CompanyKind.FUND and self.asset_type not in etf_types:
            raise ValueError("FUND company_kind is reserved for ETF types")


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
        require_korean_security_symbol(self.symbol, field="OHLCV symbol")
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
class MarketQuote:
    symbol: str
    last_price: Decimal
    currency: str
    source_timestamp: datetime
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="quote symbol")
        if self.last_price <= 0:
            raise ValueError("last_price must be positive")
        if self.currency != "KRW":
            raise ValueError("only KRW quotes are supported")
        if not self.source.strip():
            raise ValueError("quote source is required")
        source_timestamp = _utc(self.source_timestamp)
        observed_at = _utc(self.observed_at)
        if source_timestamp > observed_at:
            raise ValueError("quote source_timestamp cannot be after observed_at")
        object.__setattr__(self, "source_timestamp", source_timestamp)
        object.__setattr__(self, "observed_at", observed_at)


@dataclass(frozen=True, slots=True)
class IntradayCandle:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    currency: str
    interval: str
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="intraday candle symbol")
        if self.interval != "1m":
            raise ValueError("only 1m intraday candles are supported")
        if any(value <= 0 for value in (self.open, self.high, self.low, self.close)):
            raise ValueError("intraday candle prices must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below another intraday price")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above another intraday price")
        if self.volume < 0:
            raise ValueError("intraday candle volume cannot be negative")
        if self.currency != "KRW":
            raise ValueError("only KRW candles are supported")
        if not self.source.strip():
            raise ValueError("intraday candle source is required")
        timestamp = _utc(self.timestamp)
        observed_at = _utc(self.observed_at)
        if timestamp > observed_at:
            raise ValueError("intraday candle timestamp cannot be after observed_at")
        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "observed_at", observed_at)


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
    annual_operating_income: Decimal | None = None
    ttm_operating_income: Decimal | None = None
    ttm_period_end: date | None = None
    ttm_source_url: str | None = None

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="financial symbol")
        object.__setattr__(self, "published_at", _utc(self.published_at))
        if not self.source_url.strip():
            raise ValueError("financial source_url is required")
        if self.ttm_period_end is not None and self.ttm_period_end > self.published_at.date():
            raise ValueError("ttm_period_end cannot be after published_at")
        if self.ttm_source_url is not None and not self.ttm_source_url.strip():
            raise ValueError("ttm_source_url cannot be blank")


@dataclass(frozen=True, slots=True)
class FinancialCompanySnapshot:
    """Point-in-time specialist metrics; thresholds remain a human policy decision."""

    symbol: str
    period_end: date
    published_at: datetime
    capital_adequacy_ratio: Decimal | None
    return_on_equity: Decimal | None
    non_performing_loan_ratio: Decimal | None
    delinquency_ratio: Decimal | None
    provision_coverage_ratio: Decimal | None
    shareholder_return_note: str | None
    source_url: str

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="financial-company symbol")
        object.__setattr__(self, "published_at", _utc(self.published_at))
        for field_name in (
            "capital_adequacy_ratio",
            "non_performing_loan_ratio",
            "delinquency_ratio",
            "provision_coverage_ratio",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.shareholder_return_note is not None and not self.shareholder_return_note.strip():
            raise ValueError("shareholder_return_note cannot be blank")
        if not self.source_url.strip():
            raise ValueError("financial-company source_url is required")


@dataclass(frozen=True, slots=True)
class EtfSnapshot:
    symbol: str
    trade_date: date
    observed_at: datetime
    nav_per_share: Decimal | None
    net_assets: Decimal | None
    premium_discount_pct: Decimal | None
    tracking_error_pct: Decimal | None
    total_expense_ratio_pct: Decimal | None
    top10_weight_pct: Decimal | None
    source_url: str

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="ETF symbol")
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        if self.nav_per_share is not None and self.nav_per_share <= 0:
            raise ValueError("nav_per_share must be positive when present")
        for field_name in ("nav_per_share", "net_assets", "tracking_error_pct", "total_expense_ratio_pct"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        if self.top10_weight_pct is not None and not Decimal("0") <= self.top10_weight_pct <= Decimal("100"):
            raise ValueError("top10_weight_pct must be between 0 and 100")
        if not self.source_url.strip():
            raise ValueError("ETF source_url is required")


@dataclass(frozen=True, slots=True)
class FinancingEvent:
    symbol: str
    event_type: str
    announced_at: datetime
    dilutive: bool
    official: bool
    source_url: str
    dilution_ratio_pct: Decimal | None = None
    purpose: str | None = None
    refixing: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "announced_at", _utc(self.announced_at))
        require_korean_security_symbol(self.symbol, field="financing symbol")
        if not self.event_type.strip():
            raise ValueError("financing event_type is required")
        if not self.source_url.strip():
            raise ValueError("financing source_url is required")
        if self.dilution_ratio_pct is not None and self.dilution_ratio_pct < 0:
            raise ValueError("dilution_ratio_pct cannot be negative")
        if self.purpose is not None and not self.purpose.strip():
            raise ValueError("financing purpose cannot be blank")


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
        require_korean_security_symbol(self.symbol, field="holding symbol")
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
    market: str | None = None
    asset_type: str | None = None
    strategy: StrategyType = StrategyType.UNAVAILABLE
    expected_holding_period: HoldingPeriod = HoldingPeriod.REVIEW_REQUIRED
    catalyst_states: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    evidence_urls: tuple[str, ...] = ()
    review_tier: ReviewTier = ReviewTier.STANDARD_REVIEW

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
