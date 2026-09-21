from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum

from .backtest import event_study
from .repository import StockRepository


KST = timezone(timedelta(hours=9))


class PerformanceStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    PENDING_HORIZON = "PENDING_HORIZON"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class PerformanceRecord:
    report_id: str
    ruleset_version: str
    symbol: str
    decision: str
    strategy: str
    expected_holding_period: str
    score: Decimal
    signal_at: datetime
    evaluated_at: datetime
    trading_days: int
    status: PerformanceStatus
    entry_date: str | None
    entry_price: Decimal | None
    exit_date: str | None
    gross_return: Decimal | None
    net_return: Decimal | None

    def __post_init__(self) -> None:
        if self.signal_at.tzinfo is None or self.evaluated_at.tzinfo is None:
            raise ValueError("signal_at and evaluated_at must be timezone-aware")
        if self.evaluated_at < self.signal_at:
            raise ValueError("evaluated_at cannot be before signal_at")
        if self.trading_days < 1:
            raise ValueError("trading_days must be positive")
        if not (self.symbol.isdigit() and len(self.symbol) == 6):
            raise ValueError("symbol must be six digits")


@dataclass(frozen=True, slots=True)
class PerformanceEvaluation:
    report_id: str
    evaluated_at: datetime
    records: tuple[PerformanceRecord, ...]


def evaluate_report_performance(
    repository: StockRepository,
    *,
    report_id: str,
    evaluated_at: datetime,
    horizons: tuple[int, ...] = (5, 20, 60),
    round_trip_cost_bps: Decimal = Decimal("30"),
) -> PerformanceEvaluation:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    if not horizons or any(value < 1 for value in horizons):
        raise ValueError("horizons must contain positive trading-day counts")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons cannot contain duplicates")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")
    report = repository.get_report(report_id)
    if report is None:
        raise ValueError("report not found")
    signal_at = datetime.fromisoformat(str(report["as_of"]))
    if signal_at.tzinfo is None:
        raise ValueError("stored report as_of must be timezone-aware")
    if evaluated_at < signal_at:
        raise ValueError("evaluated_at cannot be before report as_of")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("stored report candidates must be a list")
    ruleset_version = str(report.get("ruleset_version") or "legacy-unversioned")
    signal_date = signal_at.astimezone(KST).date()
    records: list[PerformanceRecord] = []
    completed = {
        (record.symbol, record.trading_days): record
        for record in repository.list_performance_records(report_id=report_id)
        if record.status is PerformanceStatus.COMPLETE
    }
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("stored report candidate must be an object")
        symbol = str(candidate["symbol"])
        bars = repository.bars_since(symbol, start_date=signal_date, as_of=evaluated_at)
        study = event_study(
            bars,
            signal_date=signal_date,
            horizons=horizons,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for outcome in study.horizons:
            frozen = completed.get((symbol, outcome.trading_days))
            if frozen is not None:
                records.append(frozen)
                continue
            if study.entry_date is None:
                status = PerformanceStatus.PENDING_ENTRY
            elif outcome.exit_date is None:
                status = PerformanceStatus.PENDING_HORIZON
            else:
                status = PerformanceStatus.COMPLETE
            records.append(PerformanceRecord(
                report_id=report_id,
                ruleset_version=ruleset_version,
                symbol=symbol,
                decision=str(candidate.get("decision") or "UNKNOWN"),
                strategy=str(candidate.get("strategy") or "UNAVAILABLE"),
                expected_holding_period=str(
                    candidate.get("expected_holding_period") or "REVIEW_REQUIRED"
                ),
                score=Decimal(str(candidate.get("score", "0"))),
                signal_at=signal_at.astimezone(timezone.utc),
                evaluated_at=evaluated_at.astimezone(timezone.utc),
                trading_days=outcome.trading_days,
                status=status,
                entry_date=study.entry_date.isoformat() if study.entry_date else None,
                entry_price=study.entry_price,
                exit_date=outcome.exit_date.isoformat() if outcome.exit_date else None,
                gross_return=outcome.gross_return,
                net_return=outcome.net_return,
            ))
    repository.save_performance_records(records)
    stored = {
        (record.symbol, record.trading_days): record
        for record in repository.list_performance_records(report_id=report_id)
    }
    persisted = tuple(stored[(record.symbol, record.trading_days)] for record in records)
    return PerformanceEvaluation(report_id, evaluated_at.astimezone(timezone.utc), persisted)


def evaluate_all_report_performance(
    repository: StockRepository,
    *,
    evaluated_at: datetime,
    horizons: tuple[int, ...] = (5, 20, 60),
    round_trip_cost_bps: Decimal = Decimal("30"),
) -> tuple[PerformanceEvaluation, ...]:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return tuple(
        evaluate_report_performance(
            repository,
            report_id=report_id,
            evaluated_at=evaluated_at,
            horizons=horizons,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for report_id in repository.list_report_ids(as_of=evaluated_at)
    )


def summarize_performance(records: list[PerformanceRecord]) -> list[dict[str, str | int]]:
    groups: dict[tuple[str, str, str, int], list[Decimal]] = {}
    for record in records:
        if record.status is not PerformanceStatus.COMPLETE or record.net_return is None:
            continue
        key = (record.ruleset_version, record.decision, record.strategy, record.trading_days)
        groups.setdefault(key, []).append(record.net_return)
    summaries: list[dict[str, str | int]] = []
    for (ruleset_version, decision, strategy, trading_days), values in sorted(groups.items()):
        count = len(values)
        summaries.append({
            "ruleset_version": ruleset_version,
            "decision": decision,
            "strategy": strategy,
            "trading_days": trading_days,
            "sample_count": count,
            "average_net_return": str(sum(values, Decimal("0")) / count),
            "win_rate": str(Decimal(sum(value > 0 for value in values)) / count),
        })
    return summaries
