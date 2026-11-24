from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from datetime import date, datetime, timezone
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
from stock_assistant.research_data import ResearchData
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

    def test_unified_recommendation_uses_publication_and_completes_three_horizons(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = StockRepository(Path(directory) / "stock.sqlite3")
            store = ResearchData(Path(directory) / "unified-research-inputs.sqlite3", source_database=repo.database_path)
            published = datetime(2026, 1, 9, 15, 30, tzinfo=UTC)  # Saturday KST.
            report_id = "UR-" + "a" * 24
            store.put("unified_report", "ALL", {
                "report_id": report_id, "contract": "UNIFIED_RESEARCH_V1", "policy_version": "unified-example",
                "as_of": "2026-01-08T03:00:00+00:00",
                "candidates": [{"symbol": "005930", "action_decision": "BUY_RECOMMENDATION",
                                "total_score": "85", "price": {"strategy": "MOMENTUM_CONTINUATION"}},
                               {"symbol": "000660", "action_decision": "WATCH", "total_score": "74.5"}],
            }, observed_at=published)
            bars = [replace(bar, source="KRX_OPEN_API") for bar in make_bars(count=120) if bar.trade_date.weekday() < 5]
            repo.save_bars(bars)
            evaluated_at = datetime(2026, 5, 1, tzinfo=UTC)
            result, = evaluate_all_report_performance(repo, evaluated_at=evaluated_at)
            self.assertEqual(result.report_id, report_id)
            for record in result.records:
                self.assertEqual(record.signal_at, published)
                self.assertEqual(record.entry_date, "2026-01-12")
                self.assertEqual(record.entry_tradability, "TRADABLE")
                self.assertEqual(record.status, PerformanceStatus.COMPLETE)
                self.assertEqual(record.round_trip_cost_bps, Decimal("30"))
                self.assertEqual(record.net_return, record.gross_return - Decimal("0.003"))
            self.assertEqual([r.trading_days for r in result.records], [5, 20, 60])
            self.assertEqual([r["sample_count"] for r in summarize_performance(repo.list_performance_records(report_id=report_id))], [1, 1, 1])
            self.assertEqual(evaluate_report_performance(repo, report_id=report_id, evaluated_at=evaluated_at).records,
                             result.records)

    def test_unified_does_not_slide_missing_or_non_tradable_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = StockRepository(Path(directory) / "stock.sqlite3")
            store = ResearchData(Path(directory) / "unified-research-inputs.sqlite3", source_database=repo.database_path)
            published = datetime(2026, 1, 9, 3, tzinfo=UTC)
            report_id = "UR-" + "b" * 24
            store.put("unified_report", "ALL", {"report_id": report_id, "contract": "UNIFIED_RESEARCH_V1",
                "as_of": published.isoformat(), "policy_version": "unified-example",
                "candidates": [{"symbol": "005930", "action_decision": "BUY_RECOMMENDATION", "total_score": "85"}],
            }, observed_at=published)
            target = date(2026, 1, 12)
            reference = [replace(bar, source="KRX_OPEN_API") for bar in make_bars("000660") if bar.trade_date.weekday() < 5]
            store.put("market_calendar", "KR", {"source": "TOSS_SECURITIES_OPEN_API", "calendar": {
                "today": {"date": target.isoformat(), "integrated": {"regularMarket": True}},
            }}, observed_at=datetime(2026, 1, 12, 3, tzinfo=UTC))
            available = [bar for bar in reference if bar.trade_date != target]
            repo.save_bars(available + [replace(bar, symbol="005930") for bar in available])
            at = datetime(2026, 3, 3, tzinfo=UTC)
            def evaluate():
                return evaluate_report_performance(repo, report_id=report_id, evaluated_at=at, horizons=(5,)).records[0]
            missing = evaluate()
            self.assertEqual(missing.scheduled_entry_date, target.isoformat())
            self.assertEqual(missing.entry_tradability, "UNCONFIRMED")
            self.assertIsNone(missing.entry_price)
            entry = next(bar for bar in reference if bar.trade_date == target)
            repo.save_bars([replace(entry, symbol="005930", volume=0)])
            self.assertEqual(evaluate().entry_tradability, "NOT_TRADABLE")
            self.assertIsNone(evaluate().net_return)


if __name__ == "__main__":
    unittest.main()
