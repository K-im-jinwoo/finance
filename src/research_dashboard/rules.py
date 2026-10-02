from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from .contracts import instant, number

@dataclass(frozen=True)
class Policy:
    rule_version: str = "paper-ledger-v1-local-20260930"
    initial_cash: str = "10000000"
    entry_fraction: str = "0.20"
    max_positions: int = 5
    round_trip_bps: str = "30"
    # Explicit user replies on 2026-09-30 approve these two policies.
    cash_policy: str = "DEFER"
    pause_policy: str = "CANCEL_PENDING_REQUIRE_NEW"
    # Implementation proposals remain visible in experiment snapshots.
    price_boundary: str = "LT"
    policy_status: str = "LOCAL_VALIDATION_PROPOSAL"
    price_basis: str = "UNADJUSTED_CONSISTENT_NO_ACTIONS"
    proposals: tuple[str, ...] = ("LT boundary", "fees rounded HALF_UP to whole KRW", "sell before buy at same open", "pretrade assets frozen per open", "end mark only")

    def __post_init__(self):
        if number(self.initial_cash) <= 0 or number(self.entry_fraction) != Decimal("0.20") or self.max_positions != 5:
            raise ValueError("user capital/entry/limit contract")
        if not 0 <= number(self.round_trip_bps) < 10000:
            raise ValueError("invalid cost assumption")
        if self.cash_policy != "DEFER" or self.pause_policy != "CANCEL_PENDING_REQUIRE_NEW" or self.price_boundary != "LT":
            raise ValueError("unsupported policy")
        if self.policy_status != "LOCAL_VALIDATION_PROPOSAL":
            raise ValueError("unconfirmed proposals cannot be marked approved")

    def snapshot(self):
        return asdict(self)

def invalidation(security: dict, event: dict) -> list[dict]:
    """Do not parse report prose or auto-liquidate on disclosure/rank changes."""
    at = instant(event["at"])
    result = []
    price = event.get("price")
    if not price or not price.get("fresh", False) or price.get("basis") != "UNADJUSTED_CONSISTENT_NO_ACTIONS" or len(price.get("closes", [])) < 20:
        result.append({"code": "PRICE_LT_SMA20", "status": "UNKNOWN", "reason": "missing/stale/inconsistent price evidence"})
    else:
        closes = list(map(number, price["closes"][-20:]))
        mean = sum(closes) / 20
        result.append({"code": "PRICE_LT_SMA20", "status": "INVALID" if closes[-1] < mean else "VALID", "evidence_id": price["id"], "available_at": price["available_at"], "close": str(closes[-1]), "sma20": str(mean)})
    financial = event.get("financial")
    if security["kind"] != "GENERAL":
        result.append({"code": "GENERAL_FINANCIAL", "status": "NOT_APPLICABLE" if security["kind"] in {"ETF", "FINANCIAL"} else "UNKNOWN", "reason": "specialist thresholds not approved"})
    elif not financial or not financial.get("fresh", False) or not financial.get("periods_verified", False):
        result.append({"code": "GENERAL_FINANCIAL", "status": "UNKNOWN", "reason": "missing/stale/unverified periods"})
    else:
        values = [financial.get(k) for k in ("operating_income", "annual_operating_income", "ttm_operating_income", "ocf")]
        status = "INVALID" if any(v is not None and number(v) <= 0 for v in values) else "UNKNOWN" if any(v is None for v in values) else "VALID"
        result.append({"code": "GENERAL_FINANCIAL", "status": status, "evidence_id": financial["id"], "available_at": financial["available_at"]})
    result.append({"code": "DISCLOSURE", "status": "REVIEW_REQUIRED", "reason": "auto invalidation excluded from v1"})
    return result

