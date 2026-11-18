"""Price-independent financial research. This proposal never produces trade signals."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from .indicators import trend_is_stable_or_rising
from .models import AssetType, CompanyKind, Evidence, FinancialSnapshot, Security, to_json_value
from .identifiers import require_korean_security_symbol
from .validation import assert_point_in_time, validate_analysis_time


POLICY_VERSION = "financial-research-2026-10-12.1"
POLICY_STATUS = "LOCAL_VALIDATION_PROPOSAL"
WEIGHTS = {"profit": 25, "cash_flow": 25, "profit_history": 20, "turnover": 15, "free_cash_flow": 15}


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _number(value: Decimal | None, label: str) -> None:
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
        raise ValueError(f"{label} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class AnnualProfit:
    symbol: str
    period_end: date
    operating_income: Decimal
    published_at: datetime
    observed_at: datetime
    receipt_id: str
    statement_basis: str
    source_url: str

    def __post_init__(self) -> None:
        require_korean_security_symbol(self.symbol, field="annual profit symbol")
        if type(self.period_end) is not date:
            raise ValueError("annual period_end must be a date")
        _number(self.operating_income, "annual operating_income")
        if self.operating_income is None:
            raise ValueError("annual operating_income is required")
        published = _aware(self.published_at, "annual published_at")
        observed = _aware(self.observed_at, "annual observed_at")
        if self.period_end > published.date() or observed < published:
            raise ValueError("annual profit timing is invalid")
        if (not isinstance(self.receipt_id, str) or len(self.receipt_id) != 14
                or not self.receipt_id.isascii() or not self.receipt_id.isdigit()):
            raise ValueError("annual receipt_id must contain 14 digits")
        if (self.statement_basis not in {"CFS", "OFS"}
                or not isinstance(self.source_url, str) or not self.source_url.strip()):
            raise ValueError("annual statement basis and source URL are required")


@dataclass(frozen=True, slots=True)
class FinancialReview:
    symbol: str
    name: str
    as_of: datetime
    gate: str
    financial_score: Decimal | None
    score_breakdown: dict
    blocking_reasons: tuple[str, ...]
    advisories: tuple[str, ...]
    pending_checks: tuple[str, ...]
    fingerprint: str
    evaluation_id: str
    period_end: date | None
    ttm_period_end: date | None
    score_coverage: dict
    source_inputs: dict
    policy_version: str = POLICY_VERSION
    policy_status: str = POLICY_STATUS


def evaluate_financial(
    security: Security,
    financial: FinancialSnapshot | None,
    *,
    as_of: datetime,
    annual_history: tuple[AnnualProfit, ...] = (),
    fcf_growth_evidence: tuple[Evidence, ...] = (),
) -> FinancialReview:
    """Evaluate existing hard rules and explicit, fixed proposal point allocations.

    Absent three-year annual evidence earns no historical points. A legacy snapshot
    is not certified for historical replay merely because it has a publication date.
    """
    as_of = validate_analysis_time(as_of)
    blockers, advisories, pending = [], [], []
    breakdown = {key: {"points": None, "maximum": maximum, "status": "UNAVAILABLE"}
                 for key, maximum in WEIGHTS.items()}
    annual_history = tuple(sorted(annual_history, key=lambda item: (item.period_end, item.published_at,
                                                                   item.observed_at, item.receipt_id)))
    fcf_growth_evidence = tuple(sorted(fcf_growth_evidence, key=lambda item: (item.published_at, item.url)))
    payload = {"policy_version": POLICY_VERSION, "weights": WEIGHTS, "security": to_json_value(security),
               "financial": to_json_value(financial), "history": to_json_value(annual_history),
               "growth_evidence": to_json_value(fcf_growth_evidence)}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()
    gate = "PASS"
    score = None
    if (security.listed_on and security.listed_on > as_of.date()) or (
            security.delisted_on and security.delisted_on <= as_of.date()):
        gate, blockers = "EXCLUDED", ["SECURITY_NOT_LISTED_AS_OF"]
    elif security.asset_type is not AssetType.COMMON or security.company_kind is CompanyKind.FINANCIAL:
        gate, advisories = "NOT_APPLICABLE", ["SEPARATE_ASSET_POLICY_REQUIRED"]
    elif security.company_kind is not CompanyKind.GENERAL:
        gate, blockers = "VERIFY_PENDING", ["COMPANY_KIND_UNAVAILABLE"]
    elif financial is None:
        gate, blockers = "VERIFY_PENDING", ["FINANCIAL_SNAPSHOT_UNAVAILABLE"]
    else:
        if financial.symbol != security.symbol:
            raise ValueError("financial snapshot symbol does not match security")
        assert_point_in_time(as_of, published_at=financial.published_at, label="financial snapshot")
        if financial.period_end > as_of.date() or financial.period_end > financial.published_at.date():
            raise ValueError("financial period is later than its publication or as_of")
        for key in ("operating_income", "annual_operating_income", "ttm_operating_income",
                    "operating_cash_flow", "free_cash_flow"):
            _number(getattr(financial, key), key)
        for key in ("receivable_turnover", "inventory_turnover"):
            for value in getattr(financial, key):
                _number(value, key)
                if value is None or value < 0:
                    raise ValueError(f"{key} must contain non-negative finite Decimals")
        if financial.operating_income is None:
            raise ValueError("operating_income is required")

        annual, ttm = financial.annual_operating_income, financial.ttm_operating_income
        if any(value is not None and value <= 0 for value in (financial.operating_income, annual, ttm)):
            blockers.append("OPERATING_LOSS")
        if financial.operating_cash_flow is not None and financial.operating_cash_flow <= 0:
            blockers.append("OCF_NON_POSITIVE")
        exclusions = bool(blockers)
        if annual is None or ttm is None or financial.ttm_period_end is None or not financial.ttm_source_url:
            blockers.append("PROFIT_PERIODS_UNAVAILABLE")
        if financial.ttm_period_end is not None:
            if financial.ttm_period_end > as_of.date() or financial.ttm_period_end > financial.published_at.date():
                raise ValueError("TTM period is later than its publication or as_of")
            if (as_of.date() - financial.ttm_period_end).days > 185:
                blockers.append("TTM_STALE")
        if financial.operating_cash_flow is None:
            blockers.append("OCF_UNAVAILABLE")
        gate = "EXCLUDED" if exclusions else "VERIFY_PENDING" if blockers else "PASS"

        def component(key: str, points: Decimal | int, reason: str) -> None:
            breakdown[key].update(points=Decimal(points), status="AVAILABLE", reason=reason)

        if annual is not None and ttm is not None:
            component("profit", 25 if min(financial.operating_income, annual, ttm) > 0 else 0,
                      "ANNUAL_AND_TTM_POSITIVE")
        if financial.operating_cash_flow is not None:
            component("cash_flow", 25 if financial.operating_cash_flow > 0 else 0, "OCF_POSITIVE")

        history = {}
        for item in annual_history:
            if item.symbol != security.symbol:
                raise ValueError("annual profit history symbol does not match security")
            assert_point_in_time(as_of, published_at=item.published_at, observed_at=item.observed_at,
                                 label="annual profit evidence")
            previous = history.get(item.period_end)
            if previous and (previous.published_at, previous.observed_at) == (item.published_at, item.observed_at):
                if previous != item:
                    raise ValueError("conflicting annual profit versions")
            if previous is None or (item.published_at, item.observed_at) > (previous.published_at, previous.observed_at):
                history[item.period_end] = item
        ordered = sorted(history.values(), key=lambda item: item.period_end)[-3:]
        if len(ordered) == 3:
            if ordered[-1].period_end == financial.period_end and annual != ordered[-1].operating_income:
                blockers.append("ANNUAL_PROFIT_SOURCE_CONFLICT")
                if gate != "EXCLUDED":
                    gate = "VERIFY_PENDING"
            if len({item.statement_basis for item in ordered}) != 1:
                raise ValueError("annual profit history mixes CFS and OFS")
            if (len({(item.period_end.month, item.period_end.day) for item in ordered}) != 1
                    or ordered[-1].period_end.year - ordered[0].period_end.year != 2):
                raise ValueError("annual profit history requires three consecutive comparable years")
            if ((as_of.date() - ordered[-1].period_end).days > 550 or
                    (financial.period_end - ordered[-1].period_end).days > 370):
                pending.append("ANNUAL_PROFIT_HISTORY_STALE")
            else:
                incomes = tuple(item.operating_income for item in ordered)
                points = 20 if min(incomes) > 0 and trend_is_stable_or_rising(incomes) else 10 if min(incomes) > 0 else 0
                component("profit_history", points, "THREE_YEAR_VERIFIED_ANNUAL_PROFIT")
        else:
            pending.append("THREE_YEAR_PROFIT_HISTORY_UNAVAILABLE")

        receivable = trend_is_stable_or_rising(financial.receivable_turnover)
        inventory = trend_is_stable_or_rising(financial.inventory_turnover)
        if receivable is None or inventory is None:
            pending.append("TURNOVER_HISTORY_UNAVAILABLE")
        else:
            component("turnover", 15 if receivable and inventory else 0, "TURNOVER_STABLE_OR_RISING")
            if not receivable or not inventory:
                advisories.append("TURNOVER_WEAKENING")

        for evidence in fcf_growth_evidence:
            assert_point_in_time(as_of, published_at=evidence.published_at, observed_at=evidence.observed_at,
                                 label="FCF explanation")
        if financial.free_cash_flow is None:
            advisories.append("FCF_UNAVAILABLE_OPTIONAL")
        elif financial.free_cash_flow >= 0:
            component("free_cash_flow", 15, "FCF_NON_NEGATIVE")
        elif any(item.official and item.source_type == "DART"
                 and urlsplit(item.url).hostname in {"dart.fss.or.kr", "opendart.fss.or.kr"}
                 and "FCF_NEGATIVE_DUE_TO_GROWTH_INVESTMENT" in item.facts
                 and f"ISSUER:{security.symbol}" in item.facts
                 and f"FCF_PERIOD_END:{financial.period_end.isoformat()}" in item.facts
                 for item in fcf_growth_evidence):
            component("free_cash_flow", 10, "FCF_GROWTH_INVESTMENT_VERIFIED")
            advisories.append("FCF_NEGATIVE_GROWTH_INVESTMENT")
        else:
            component("free_cash_flow", 0, "FCF_NEGATIVE_CAUSE_UNVERIFIED")
            pending.append("FCF_CAUSE_REVIEW_REQUIRED")
        advisories.append("LEGACY_FINANCIAL_AVAILABILITY_AND_REVISION_UNCERTIFIED")
        if gate == "PASS":
            score = sum((item["points"] or Decimal("0") for item in breakdown.values()), Decimal("0"))

    pending = list(dict.fromkeys(blockers + pending)) if gate == "VERIFY_PENDING" else list(dict.fromkeys(pending))
    if gate in {"EXCLUDED", "NOT_APPLICABLE"}:
        pending = []
    identity = {"fingerprint": fingerprint, "gate": gate, "score": to_json_value(score),
                "breakdown": to_json_value(breakdown), "blocking": blockers, "pending": pending}
    evaluation_id = "F-" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    available = [key for key, value in breakdown.items() if value["status"] == "AVAILABLE"]
    return FinancialReview(
        security.symbol, security.name, as_of, gate, score, breakdown, tuple(blockers),
        tuple(advisories), tuple(pending), fingerprint, evaluation_id,
        financial.period_end if financial else None, financial.ttm_period_end if financial else None,
        {"available_components": available, "available_maximum": sum(WEIGHTS[key] for key in available),
         "total_maximum": 100, "historical_replay_certified": False},
        payload,
    )
