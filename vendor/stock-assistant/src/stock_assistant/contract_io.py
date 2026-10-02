from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import (
    AssetType,
    Catalyst,
    CatalystStatus,
    CompanyKind,
    EtfSnapshot,
    Evidence,
    FinancialCompanySnapshot,
    FinancialSnapshot,
    FinancingEvent,
    ManagementRisk,
    Market,
    OHLCV,
    Security,
)


@dataclass(frozen=True, slots=True)
class ScreenRequest:
    as_of: datetime
    security: Security
    bars: list[OHLCV]
    financial: FinancialSnapshot | None
    financial_company: FinancialCompanySnapshot | None
    etf_snapshot: EtfSnapshot | None
    financing_events: tuple[FinancingEvent, ...]
    management_risks: tuple[ManagementRisk, ...]
    catalysts: tuple[Catalyst, ...]


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return value


def _datetime(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601 date") from exc


def _decimal_tuple(value: Any, field: str) -> tuple[Decimal, ...]:
    if value is None:
        return ()
    return tuple(Decimal(str(item)) for item in _array(value, field))


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def parse_security(value: Any) -> Security:
    item = _object(value, "security")
    return Security(
        symbol=str(item["symbol"]), name=str(item["name"]), market=Market(str(item["market"])),
        asset_type=AssetType(str(item["asset_type"])), company_kind=CompanyKind(str(item["company_kind"])),
        listed_on=_date(item["listed_on"], "security.listed_on") if item.get("listed_on") else None,
        delisted_on=_date(item["delisted_on"], "security.delisted_on") if item.get("delisted_on") else None,
    )


def parse_bar(value: Any) -> OHLCV:
    item = _object(value, "bar")
    return OHLCV(
        symbol=str(item["symbol"]), trade_date=_date(item["trade_date"], "bar.trade_date"),
        open=Decimal(str(item["open"])), high=Decimal(str(item["high"])),
        low=Decimal(str(item["low"])), close=Decimal(str(item["close"])),
        volume=int(item["volume"]), source=str(item["source"]),
        observed_at=_datetime(item["observed_at"], "bar.observed_at"),
    )


def parse_evidence(value: Any) -> Evidence:
    item = _object(value, "evidence")
    facts = tuple(str(fact) for fact in _array(item.get("facts", []), "evidence.facts"))
    return Evidence(
        source_type=str(item["source_type"]), title=str(item["title"]), url=str(item["url"]),
        published_at=_datetime(item["published_at"], "evidence.published_at"),
        observed_at=_datetime(item["observed_at"], "evidence.observed_at"),
        official=bool(item["official"]), facts=facts,
    )


def parse_financial(value: Any) -> FinancialSnapshot | None:
    if value is None:
        return None
    item = _object(value, "financial")
    ocf = item.get("operating_cash_flow")
    fcf = item.get("free_cash_flow")
    return FinancialSnapshot(
        symbol=str(item["symbol"]), period_end=_date(item["period_end"], "financial.period_end"),
        published_at=_datetime(item["published_at"], "financial.published_at"),
        operating_income=Decimal(str(item["operating_income"])),
        operating_cash_flow=Decimal(str(ocf)) if ocf is not None else None,
        free_cash_flow=Decimal(str(fcf)) if fcf is not None else None,
        receivable_turnover=_decimal_tuple(item.get("receivable_turnover"), "financial.receivable_turnover"),
        inventory_turnover=_decimal_tuple(item.get("inventory_turnover"), "financial.inventory_turnover"),
        source_url=str(item["source_url"]),
        annual_operating_income=_optional_decimal(item, "annual_operating_income"),
        ttm_operating_income=_optional_decimal(item, "ttm_operating_income"),
        ttm_period_end=(
            _date(item["ttm_period_end"], "financial.ttm_period_end")
            if item.get("ttm_period_end") else None
        ),
        ttm_source_url=(str(item["ttm_source_url"]) if item.get("ttm_source_url") else None),
    )


def _optional_decimal(item: dict[str, Any], field: str) -> Decimal | None:
    value = item.get(field)
    return _decimal(value, field) if value is not None else None


def parse_financial_company(value: Any) -> FinancialCompanySnapshot | None:
    if value is None:
        return None
    item = _object(value, "financial_company")
    return FinancialCompanySnapshot(
        symbol=str(item["symbol"]),
        period_end=_date(item["period_end"], "financial_company.period_end"),
        published_at=_datetime(item["published_at"], "financial_company.published_at"),
        capital_adequacy_ratio=_optional_decimal(item, "capital_adequacy_ratio"),
        return_on_equity=_optional_decimal(item, "return_on_equity"),
        non_performing_loan_ratio=_optional_decimal(item, "non_performing_loan_ratio"),
        delinquency_ratio=_optional_decimal(item, "delinquency_ratio"),
        provision_coverage_ratio=_optional_decimal(item, "provision_coverage_ratio"),
        shareholder_return_note=(
            str(item["shareholder_return_note"])
            if item.get("shareholder_return_note") is not None else None
        ),
        source_url=str(item["source_url"]),
    )


def parse_etf_snapshot(value: Any) -> EtfSnapshot | None:
    if value is None:
        return None
    item = _object(value, "etf_snapshot")
    return EtfSnapshot(
        symbol=str(item["symbol"]),
        trade_date=_date(item["trade_date"], "etf_snapshot.trade_date"),
        observed_at=_datetime(item["observed_at"], "etf_snapshot.observed_at"),
        nav_per_share=_optional_decimal(item, "nav_per_share"),
        net_assets=_optional_decimal(item, "net_assets"),
        premium_discount_pct=_optional_decimal(item, "premium_discount_pct"),
        tracking_error_pct=_optional_decimal(item, "tracking_error_pct"),
        total_expense_ratio_pct=_optional_decimal(item, "total_expense_ratio_pct"),
        top10_weight_pct=_optional_decimal(item, "top10_weight_pct"),
        source_url=str(item["source_url"]),
    )


def parse_screen_request(value: Any) -> ScreenRequest:
    item = _object(value, "screen request")
    financing = []
    for raw in _array(item.get("financing_events", []), "financing_events"):
        event = _object(raw, "financing event")
        financing.append(FinancingEvent(
            symbol=str(event["symbol"]), event_type=str(event["event_type"]),
            announced_at=_datetime(event["announced_at"], "financing.announced_at"),
            dilutive=bool(event["dilutive"]), official=bool(event["official"]),
            source_url=str(event["source_url"]),
            dilution_ratio_pct=(
                _decimal(event["dilution_ratio_pct"], "financing.dilution_ratio_pct")
                if event.get("dilution_ratio_pct") is not None else None
            ),
            purpose=str(event["purpose"]) if event.get("purpose") is not None else None,
            refixing=bool(event["refixing"]) if event.get("refixing") is not None else None,
        ))
    management = []
    for raw in _array(item.get("management_risks", []), "management_risks"):
        risk = _object(raw, "management risk")
        management.append(ManagementRisk(
            symbol=str(risk["symbol"]), confirmed=bool(risk["confirmed"]), category=str(risk["category"]),
            evidence=tuple(parse_evidence(value) for value in _array(risk.get("evidence", []), "management.evidence")),
        ))
    catalysts = []
    for raw in _array(item.get("catalysts", []), "catalysts"):
        catalyst = _object(raw, "catalyst")
        catalysts.append(Catalyst(
            symbol=str(catalyst["symbol"]), category=str(catalyst["category"]),
            status=CatalystStatus(str(catalyst["status"])),
            announced_at=_datetime(catalyst["announced_at"], "catalyst.announced_at"),
            valid_until=_datetime(catalyst["valid_until"], "catalyst.valid_until") if catalyst.get("valid_until") else None,
            evidence=tuple(parse_evidence(value) for value in _array(catalyst.get("evidence", []), "catalyst.evidence")),
        ))
    return ScreenRequest(
        as_of=_datetime(item["as_of"], "as_of"),
        security=parse_security(item["security"]),
        bars=[parse_bar(value) for value in _array(item["bars"], "bars")],
        financial=parse_financial(item.get("financial")),
        financial_company=parse_financial_company(item.get("financial_company")),
        etf_snapshot=parse_etf_snapshot(item.get("etf_snapshot")),
        financing_events=tuple(financing), management_risks=tuple(management), catalysts=tuple(catalysts),
    )
