from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.models import Decision, ScreeningResult
from stock_assistant.performance import (
    PerformanceStatus,
    evaluate_all_report_performance,
    evaluate_report_performance,
    summarize_performance,
)
from stock_assistant.reports import build_candidate_report, report_to_dict
from stock_assistant.repository import StockRepository
from tests.helpers import make_bars


UTC = timezone.utc


class PerformanceTests(unittest.TestCase):
    def _repository_with_report(self, directory: str) -> tuple[StockRepository, str]:
        repository = StockRepository(Path(directory) / "stock.sqlite3")
        signal_at = datetime(2026, 1, 10, 12, tzinfo=UTC)
        result = ScreeningResult(
            "005930", signal_at, Decision.CANDIDATE, Decimal("75"),
            ("TEST_SIGNAL",), (), ("REVIEW",), {}, "삼성전자",
        )
        report = build_candidate_report(signal_at, [result])
        repository.save_report(report.report_id, report.as_of.isoformat(), report_to_dict(report))
        repository.save_bars(make_bars("005930", count=80))
        return repository, report.report_id

    def test_evaluation_uses_next_session_and_persists_cost_adjusted_returns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, report_id = self._repository_with_report(directory)
            evaluation = evaluate_report_performance(
                repository,
                report_id=report_id,
                evaluated_at=datetime(2026, 3, 31, tzinfo=UTC),
                horizons=(5, 20),
                round_trip_cost_bps=Decimal("30"),
            )
            self.assertEqual(len(evaluation.records), 2)
            first = evaluation.records[0]
            self.assertEqual(first.status, PerformanceStatus.COMPLETE)
            self.assertEqual(first.entry_date, "2026-01-11")
            self.assertEqual(first.exit_date, "2026-01-16")
            self.assertEqual(first.net_return, first.gross_return - Decimal("0.003"))
            stored = repository.list_performance_records(report_id=report_id)
            self.assertEqual(stored, list(evaluation.records))
            summary = summarize_performance(stored)
            self.assertEqual(summary[0]["sample_count"], 1)
            self.assertEqual(summary[0]["ruleset_version"], "2026-09-28.1")
            self.assertEqual(summary[0]["strategy"], "UNAVAILABLE")

    def test_not_yet_observed_future_bars_remain_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, report_id = self._repository_with_report(directory)
            evaluation = evaluate_report_performance(
                repository,
                report_id=report_id,
                evaluated_at=datetime(2026, 1, 12, tzinfo=UTC),
                horizons=(5,),
            )
            record = evaluation.records[0]
            self.assertEqual(record.status, PerformanceStatus.PENDING_HORIZON)
            self.assertIsNone(record.exit_date)
            self.assertIsNone(record.net_return)

    def test_completed_outcome_remains_frozen_when_source_history_is_revised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, report_id = self._repository_with_report(directory)
            evaluated_at = datetime(2026, 3, 31, tzinfo=UTC)
            first = evaluate_report_performance(
                repository, report_id=report_id, evaluated_at=evaluated_at, horizons=(5,),
            )
            bars = make_bars("005930", count=16)
            original = bars[-1]
            from stock_assistant.models import OHLCV
            revised = OHLCV(
                original.symbol, original.trade_date, original.open, original.high + Decimal("10000"),
                original.low, original.close + Decimal("10000"), original.volume,
                original.source, datetime(2026, 4, 1, tzinfo=UTC),
            )
            repository.save_bars([revised])
            second = evaluate_report_performance(
                repository,
                report_id=report_id,
                evaluated_at=datetime(2026, 4, 2, tzinfo=UTC),
                horizons=(5,),
            )
            self.assertEqual(first.records, second.records)

    def test_evaluate_all_ignores_reports_created_after_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, report_id = self._repository_with_report(directory)
            evaluations = evaluate_all_report_performance(
                repository,
                evaluated_at=datetime(2026, 1, 12, tzinfo=UTC),
                horizons=(5,),
            )
            self.assertEqual([item.report_id for item in evaluations], [report_id])


if __name__ == "__main__":
    unittest.main()
