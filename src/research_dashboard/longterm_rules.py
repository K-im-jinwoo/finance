"""Deterministic long-term paper accounting and UR decision rules."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

from stock_assistant.identifiers import is_korean_security_symbol


CONTRACT = "UNIFIED_RESEARCH_V1"
RISK_CATEGORIES = {"MANAGEMENT", "DILUTION", "BUSINESS_DAMAGE"}
RISK_TAGS = ("경영·감사(", "희석(", "사업 훼손(")


def decimal_or_none(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class LongTermPolicy:
    initial_cash: Decimal = Decimal("10000000")
    max_positions: int = 5
    entry_fraction: Decimal = Decimal("0.20")
    side_cost_bps: int = 15

    def fee(self, amount: Decimal) -> Decimal:
        return (amount * self.side_cost_bps / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP)

    def quantity(self, nav: Decimal, price: Decimal) -> int:
        budget = nav * self.entry_fraction
        # A rounded fee can be up to half a won below its unrounded value.
        upper = min(budget / price, (budget + Decimal("0.5")) / (price * (1 + Decimal(self.side_cost_bps) / 10000)))
        high = int(upper.to_integral_value(rounding=ROUND_FLOOR))
        low = 0
        while low < high:
            quantity = (low + high + 1) // 2
            cost = price * quantity
            if cost + self.fee(cost) <= budget:
                low = quantity
            else:
                high = quantity - 1
        return low

    def snapshot(self) -> dict:
        return {"initial_cash": str(self.initial_cash), "max_positions": self.max_positions,
                "entry_fraction": str(self.entry_fraction), "round_trip_bps": 2 * self.side_cost_bps,
                "execution": "NEXT_TRADING_SESSION_CLOSE"}


def buy_candidates(report, limit):
    candidates = sorted(
        (row for row in report.get("candidates", []) if isinstance(row, dict)
         and is_korean_security_symbol(row.get("symbol")) and row.get("action_decision") == "BUY_RECOMMENDATION"),
        key=lambda row: row["rank"] if type(row.get("rank")) is int and row["rank"] > 0 else float("inf"),
    )
    unique = {}
    for row in candidates:
        unique.setdefault(row["symbol"], row)
    return list(unique.values())[:limit]


def exit_reason(row):
    if row.get("action_decision") == "EXCLUDED":
        return "EXCLUDED"
    penalty = decimal_or_none(row.get("risk_penalty"))
    if penalty is not None and penalty > 0:
        return "RISK_PENALTY"
    if row.get("risk_signals") or row.get("hard_exclusions") or any(
        isinstance(check, str) and check.startswith("MATERIAL_EVENT_REVIEW_REQUIRED:")
        for check in row.get("pending_checks") or ()
    ):
        return "RISK_SIGNAL"
    for field in ("review_signals", "news_events", "risk_headlines"):
        for signal in row.get(field) or ():
            if isinstance(signal, dict) and signal.get("category") in RISK_CATEGORIES and (
                signal.get("status") in {"ACTIVE_RISK", "UNVERIFIED_RISK"}
                or signal.get("is_risk_signal") is True or field == "review_signals"
            ):
                return "RISK_SIGNAL"
    # The canonical UR action evaluator stores review categories in warning tags.
    if any(isinstance(warning, str) and any(tag in warning for tag in RISK_TAGS)
           for warning in row.get("warnings") or ()):
        return "RISK_SIGNAL"
    return None
