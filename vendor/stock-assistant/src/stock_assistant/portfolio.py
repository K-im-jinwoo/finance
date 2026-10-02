from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Decision, Holding, ScreeningResult, ThesisCard, ThesisStatus


@dataclass(frozen=True, slots=True)
class AggregatedPosition:
    symbol: str
    quantity: Decimal
    average_price: Decimal
    brokers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PositionReview:
    symbol: str
    current_price: Decimal
    return_rate: Decimal
    thesis_status: ThesisStatus
    actions: tuple[str, ...]
    reasons: tuple[str, ...]
    additional_checks: tuple[str, ...]


def aggregate_holdings(holdings: list[Holding]) -> list[AggregatedPosition]:
    buckets: dict[str, list[Holding]] = {}
    for holding in holdings:
        buckets.setdefault(holding.symbol, []).append(holding)
    positions: list[AggregatedPosition] = []
    for symbol, items in sorted(buckets.items()):
        quantity = sum((item.quantity for item in items), Decimal("0"))
        cost = sum((item.quantity * item.average_price for item in items), Decimal("0"))
        positions.append(
            AggregatedPosition(
                symbol=symbol,
                quantity=quantity,
                average_price=cost / quantity,
                brokers=tuple(sorted({item.broker for item in items})),
            )
        )
    return positions


def review_position(
    position: AggregatedPosition,
    *,
    current_price: Decimal,
    thesis: ThesisCard,
    screening: ScreeningResult,
    invalidation_observed: bool = False,
) -> PositionReview:
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    if position.symbol != thesis.symbol or position.symbol != screening.symbol:
        raise ValueError("position, thesis, and screening symbols must match")
    return_rate = current_price / position.average_price - Decimal("1")
    actions: list[str] = []
    reasons: list[str] = []
    checks = list(thesis.additional_check_conditions) + list(screening.checks)

    thesis_status = thesis.status
    if invalidation_observed or thesis.status is ThesisStatus.INVALIDATED:
        thesis_status = ThesisStatus.INVALIDATED
        actions.append("FULL_EXIT_REVIEW")
        reasons.append("매수 논리 무효화 조건이 관찰됨")
    elif screening.decision is Decision.EXCLUDED:
        actions.append("FULL_OR_PARTIAL_EXIT_REVIEW")
        reasons.append("현재 정량 필수조건 또는 확인된 위험 기준을 통과하지 못함")
    elif return_rate >= Decimal("0.10"):
        actions.extend(("PARTIAL_PROFIT_REVIEW", "HOLD_REMAINDER_WITH_INVALIDATION"))
        reasons.append("수익 구간에서 일부 이익을 확정하고 잔여분은 논리 무효화 조건으로 관리")
    elif return_rate < 0:
        actions.append("DO_NOT_AVERAGE_DOWN_WITHOUT_REVALIDATION")
        reasons.append("손실 자체는 추가매수 근거가 아니며 재료·재무·가격 조건을 다시 검증해야 함")
    else:
        actions.append("HOLD_WITH_REVIEW_CONDITIONS")
        reasons.append("매수 논리가 유지되는 동안 추가 확인 조건을 추적")

    if screening.decision is Decision.BUY_HOLD:
        actions.append("NEW_BUY_HOLD")
        reasons.append("추가 확인 조건이 남아 신규 매수는 보류")

    return PositionReview(
        symbol=position.symbol,
        current_price=current_price,
        return_rate=return_rate,
        thesis_status=thesis_status,
        actions=tuple(dict.fromkeys(actions)),
        reasons=tuple(dict.fromkeys(reasons)),
        additional_checks=tuple(dict.fromkeys(checks)),
    )

