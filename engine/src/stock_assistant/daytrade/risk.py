"""Session time and independent, reason-scoped risk locks."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from enum import StrEnum

from .contracts import KST, instant, number


class Phase(StrEnum):
    OPEN = "OPEN"
    ENTRY_CLOSED = "ENTRY_CLOSED"
    TIME_EXITING = "TIME_EXITING"
    CLOSED = "CLOSED"


LOCKS = frozenset({"LOSS_LOCKED", "RATE_LIMIT_DEGRADED", "DATA_DEGRADED", "PRIOR_DAY_UNFLATTENED"})


@dataclass
class RiskState:
    session_date: str | None = None
    phase: Phase = Phase.CLOSED
    reasons: dict[str, list[str]] = field(default_factory=dict)
    last_at: str | None = None
    opening_equity: str | None = None
    calendar_verified: bool = False

    @property
    def locks(self):
        return frozenset(self.reasons)

    @property
    def candidate_entry_allowed(self):
        # Eligibility only; the execution boundary remains OBSERVE_ONLY.
        return self.phase == Phase.OPEN and self.calendar_verified and not self.locks

    @property
    def exit_monitor_required(self):
        return self.phase in {Phase.TIME_EXITING, Phase.CLOSED} or bool(self.locks)

    def lock(self, name, reason):
        if name not in LOCKS or not isinstance(reason, str) or not reason:
            raise ValueError("known risk lock and reason required")
        self.reasons[name] = sorted(set(self.reasons.get(name, []) + [reason]))

    def recover(self, name, reason, *, verified=False, has_positions=False):
        if name not in LOCKS or name == "LOSS_LOCKED" or not verified or (name == "PRIOR_DAY_UNFLATTENED" and has_positions):
            raise ValueError("risk lock cannot be cleared")
        values = set(self.reasons.get(name, [])) - {reason}
        if values:
            self.reasons[name] = sorted(values)
        else:
            self.reasons.pop(name, None)

    def advance(self, at, *, has_positions=False, opening_equity=None):
        now = instant(at)
        if self.last_at is not None and now < instant(self.last_at):
            raise ValueError("risk time cannot move backwards")
        local = now.astimezone(KST)
        day = local.date().isoformat()
        if day != self.session_date:
            if self.session_date is not None and has_positions:
                self.lock("PRIOR_DAY_UNFLATTENED", self.session_date)
            # Only the daily loss lock resets with a genuinely new date.
            self.reasons.pop("LOSS_LOCKED", None)
            self.session_date, self.calendar_verified = day, False
            self.opening_equity = str(number(opening_equity, positive=True)) if opening_equity is not None else None
        clock = local.time().replace(tzinfo=None)
        self.phase = (Phase.CLOSED if clock < time(9) or clock >= time(15, 30) else
                      Phase.TIME_EXITING if clock >= time(15, 15) else
                      Phase.ENTRY_CLOSED if clock >= time(14, 50) else Phase.OPEN)
        self.last_at = now.isoformat()

    def mark(self, equity):
        if equity is None or self.opening_equity is None:
            self.lock("DATA_DEGRADED", "MARK_UNCERTAIN")
            return
        # Negative net liquidation equity is a valid loss, not an invalid input.
        value = Decimal(str(equity))
        if not value.is_finite():
            raise ValueError("finite equity required")
        if value <= Decimal(self.opening_equity) * Decimal("0.97"):
            self.lock("LOSS_LOCKED", "DAILY_NET_EQUITY_LOSS")

    def record(self):
        return {"session_date": self.session_date, "phase": str(self.phase), "reasons": self.reasons,
                "last_at": self.last_at, "opening_equity": self.opening_equity,
                "calendar_verified": self.calendar_verified}

    @classmethod
    def restore(cls, record):
        return cls(record["session_date"], Phase(record["phase"]),
                   {k: list(v) for k, v in record["reasons"].items()}, record["last_at"],
                   record["opening_equity"], record["calendar_verified"])
