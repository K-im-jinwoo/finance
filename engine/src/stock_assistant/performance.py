from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from .backtest import SWING_COST_BPS, SWING_DECISION, SWING_HORIZONS, SWING_POLICY, event_study
from .identifiers import require_korean_security_symbol
from .repository import StockRepository
from .research_data import ResearchData


KST = timezone(timedelta(hours=9))


class PerformanceStatus(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    UNTRADABLE_ENTRY = "UNTRADABLE_ENTRY"
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
    scheduled_entry_date: str | None = None
    entry_tradability: str | None = None
    round_trip_cost_bps: Decimal | None = None

    def __post_init__(self) -> None:
        if self.signal_at.tzinfo is None or self.evaluated_at.tzinfo is None:
            raise ValueError("signal_at and evaluated_at must be timezone-aware")
        if self.evaluated_at < self.signal_at:
            raise ValueError("evaluated_at cannot be before signal_at")
        if self.trading_days < 1:
            raise ValueError("trading_days must be positive")
        require_korean_security_symbol(self.symbol)


@dataclass(frozen=True, slots=True)
class PerformanceEvaluation:
    report_id: str
    evaluated_at: datetime
    records: tuple[PerformanceRecord, ...]


def _research_store(repository, research_database):
    path = research_database or repository.database_path.with_name("unified-research-inputs.sqlite3")
    if research_database is None and not path.is_file():
        return None
    return ResearchData(path, source_database=repository.database_path, read_only=True)


def _market_sessions(repository, store, *, after, as_of):
    sessions = set(repository.krx_sessions(after=after, as_of=as_of))
    through = as_of.astimezone(KST).date()
    for record in store.records("market_calendar", "KR", as_of=as_of):
        payload = record["payload"]
        if payload.get("source") != "TOSS_SECURITIES_OPEN_API":
            continue
        calendar = payload["calendar"]
        for field in ("today", "previousBusinessDay"):
            session = calendar.get(field, {})
            if session.get("integrated", {}).get("regularMarket"):
                day = date.fromisoformat(session["date"])
                if after < day <= through:
                    sessions.add(day)
    return tuple(sorted(sessions))


def evaluate_report_performance(
    repository: StockRepository,
    *,
    report_id: str,
    evaluated_at: datetime,
    horizons: tuple[int, ...] = (5, 20, 60),
    round_trip_cost_bps: Decimal = Decimal("30"),
    research_database: Path | None = None,
) -> PerformanceEvaluation:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    market_sessions = None
    swing = report_id.startswith('SW-')
    if swing or report_id.startswith("UR-"):
        store = _research_store(repository, research_database)
        matching = store.performance_reports(as_of=evaluated_at, report_id=report_id, swing=swing) if store else []
        report = matching[0] if matching else None
        if report is not None:
            signal_date = datetime.fromisoformat(report["published_at"]).astimezone(KST).date()
            market_sessions = _market_sessions(repository, store, after=signal_date, as_of=evaluated_at)
    else:
        report = repository.get_report(report_id)
    if report is None:
        raise ValueError("report not found")
    if swing:
        horizons, round_trip_cost_bps = SWING_HORIZONS, SWING_COST_BPS
    return _evaluate_report(repository, report, evaluated_at=evaluated_at, horizons=horizons,
                            round_trip_cost_bps=round_trip_cost_bps, market_sessions=market_sessions)


def _evaluate_report(repository, report, *, evaluated_at, horizons, round_trip_cost_bps, market_sessions=None):
    if not horizons or any(value < 1 for value in horizons):
        raise ValueError("horizons must contain positive trading-day counts")
    if len(set(horizons)) != len(horizons):
        raise ValueError("horizons cannot contain duplicates")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps cannot be negative")
    report_id = report["report_id"]
    unified = market_sessions is not None
    signal_at = datetime.fromisoformat(report["published_at"] if unified else str(report["as_of"]))
    if signal_at.tzinfo is None:
        raise ValueError("stored report as_of must be timezone-aware")
    if evaluated_at < signal_at:
        raise ValueError("evaluated_at cannot be before report as_of")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("stored report candidates must be a list")
    ruleset_version = str(report.get("ruleset_version") or "legacy-unversioned")
    eligible_decision = SWING_DECISION if ruleset_version == SWING_POLICY else 'BUY_RECOMMENDATION'
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
        if unified and candidate.get("action_decision") != eligible_decision:
            continue
        symbol = str(candidate["symbol"])
        if all((symbol, horizon) in completed for horizon in horizons):
            records.extend(completed[(symbol, horizon)] for horizon in horizons)
            continue
        price_cutoff = evaluated_at.astimezone(KST) if unified else evaluated_at
        bars = repository.bars_since(symbol, start_date=signal_date, as_of=price_cutoff)
        if unified:
            bars = [bar for bar in bars if bar.source == "KRX_OPEN_API"]
        study = event_study(
            bars,
            signal_date=signal_date,
            horizons=horizons,
            round_trip_cost_bps=round_trip_cost_bps,
            market_sessions=market_sessions,
        )
        for outcome in study.horizons:
            frozen = completed.get((symbol, outcome.trading_days))
            if frozen is not None:
                records.append(frozen)
                continue
            if study.entry_tradability == "NOT_TRADABLE":
                status = PerformanceStatus.UNTRADABLE_ENTRY
            elif study.entry_date is None:
                status = PerformanceStatus.PENDING_ENTRY
            elif outcome.exit_date is None:
                status = PerformanceStatus.PENDING_HORIZON
            else:
                status = PerformanceStatus.COMPLETE
            records.append(PerformanceRecord(
                report_id=report_id,
                ruleset_version=ruleset_version,
                symbol=symbol,
                decision=str(candidate.get("action_decision" if unified else "decision") or "UNKNOWN"),
                strategy=str(((candidate.get("price") or {}).get("strategy") if unified else candidate.get("strategy")) or "UNAVAILABLE"),
                expected_holding_period=str(
                    candidate.get("expected_holding_period") or "REVIEW_REQUIRED"
                ),
                score=Decimal(str(candidate.get("total_score" if unified else "score", "0"))),
                signal_at=signal_at.astimezone(timezone.utc),
                evaluated_at=evaluated_at.astimezone(timezone.utc),
                trading_days=outcome.trading_days,
                status=status,
                entry_date=study.entry_date.isoformat() if study.entry_date else None,
                entry_price=study.entry_price,
                exit_date=outcome.exit_date.isoformat() if outcome.exit_date else None,
                gross_return=outcome.gross_return,
                net_return=outcome.net_return,
                scheduled_entry_date=study.scheduled_entry_date.isoformat() if study.scheduled_entry_date else None,
                entry_tradability=study.entry_tradability,
                round_trip_cost_bps=round_trip_cost_bps,
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
    research_database: Path | None = None,
) -> tuple[PerformanceEvaluation, ...]:
    if evaluated_at.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    legacy = tuple(
        evaluate_report_performance(
            repository,
            report_id=report_id,
            evaluated_at=evaluated_at,
            horizons=horizons,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for report_id in repository.list_report_ids(as_of=evaluated_at)
    )
    store = _research_store(repository, research_database)
    reports = store.performance_reports(as_of=evaluated_at) if store else []
    if not reports:
        return legacy
    earliest = min(datetime.fromisoformat(report["published_at"]).astimezone(KST).date() for report in reports)
    sessions = _market_sessions(repository, store, after=earliest, as_of=evaluated_at)
    return legacy + tuple(_evaluate_report(repository, report, evaluated_at=evaluated_at, horizons=horizons,
                                           round_trip_cost_bps=round_trip_cost_bps, market_sessions=sessions)
                          for report in reports)


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


def evaluate_swing_performance(repository, *, evaluated_at, research_database=None) -> dict:
    """Evaluate only swing publications and always show the three policy horizons."""
    if evaluated_at.tzinfo is None:
        raise ValueError('evaluated_at must be timezone-aware')
    store = _research_store(repository, research_database)
    reports = store.performance_reports(as_of=evaluated_at, swing=True) if store else []
    records = []
    if reports:
        earliest = min(datetime.fromisoformat(report['published_at']).astimezone(KST).date() for report in reports)
        sessions = _market_sessions(repository, store, after=earliest, as_of=evaluated_at)
        for report in reports:
            evaluation = _evaluate_report(repository, report, evaluated_at=evaluated_at, horizons=SWING_HORIZONS,
                                          round_trip_cost_bps=SWING_COST_BPS, market_sessions=sessions)
            records.extend(evaluation.records)
    averages = {row['trading_days']: row for row in summarize_performance(records)}
    summary = []
    for days in SWING_HORIZONS:
        rows = [row for row in records if row.trading_days == days]
        summary.append({'trading_days': days, 'sample_count': 0, 'average_net_return': None, 'win_rate': None,
                        **averages.get(days, {}),
                        'pending_entry': sum(row.status is PerformanceStatus.PENDING_ENTRY for row in rows),
                        'untradable_entry': sum(row.status is PerformanceStatus.UNTRADABLE_ENTRY for row in rows),
                        'pending_horizon': sum(row.status is PerformanceStatus.PENDING_HORIZON for row in rows)})
    return {'contract': SWING_POLICY, 'evaluated_at': evaluated_at.astimezone(timezone.utc).isoformat(),
            'evaluated_reports': len(reports), 'round_trip_cost_bps': str(SWING_COST_BPS), 'summary': summary}
