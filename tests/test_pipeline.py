from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.models import AssetType, CompanyKind, EtfSnapshot, FinancialSnapshot, Market, Security
from stock_assistant.pipeline import CandidatePipeline
from stock_assistant.repository import StockRepository
from tests.helpers import make_bars


UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, 12, tzinfo=UTC)


class PipelineTests(unittest.TestCase):
    def test_repository_pipeline_selects_five_and_persists_same_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            securities = [
                Security(
                    f"{index:06d}", f"회사-{index}", Market.KOSPI,
                    AssetType.COMMON, CompanyKind.GENERAL, date(2020, 1, 1),
                )
                for index in range(1, 7)
            ]
            repository.save_securities(securities)
            for security in securities:
                repository.save_bars(make_bars(security.symbol))
                repository.save_financial_snapshot(FinancialSnapshot(
                    security.symbol,
                    date(2025, 12, 31),
                    datetime(2026, 8, 14, tzinfo=UTC),
                    Decimal("100"), Decimal("80"), Decimal("60"),
                    (Decimal("8"), Decimal("8.1")),
                    (Decimal("6"), Decimal("6.2")),
                    "https://dart.fss.or.kr/example",
                    annual_operating_income=Decimal("100"),
                    ttm_operating_income=Decimal("120"),
                    ttm_period_end=date(2026, 6, 30),
                    ttm_source_url="https://dart.fss.or.kr/ttm",
                ))

            first = CandidatePipeline(repository).run(as_of=AS_OF)
            second = CandidatePipeline(repository).run(as_of=AS_OF)
            self.assertEqual(first.scanned, 6)
            self.assertEqual(first.missing_financing_histories, 6)
            self.assertEqual(first.missing_management_histories, 6)
            self.assertEqual(first.missing_catalyst_histories, 6)
            self.assertEqual(len(first.report.candidates), 5)
            self.assertEqual(first.report.report_id, second.report.report_id)
            stored = repository.get_report(first.report.report_id)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["report_id"], first.report.report_id)
            self.assertEqual(
                CandidatePipeline(repository).ranked_symbols(as_of=AS_OF, limit=3),
                ["000001", "000002", "000003"],
            )

    def test_pipeline_reports_missing_data_instead_of_inventing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([
                Security(
                    "005930", "삼성전자", Market.KOSPI,
                    AssetType.COMMON, CompanyKind.UNKNOWN, date(1975, 6, 11),
                )
            ])
            repository.save_bars(make_bars("005930", count=20))
            summary = CandidatePipeline(repository).run(as_of=AS_OF)
            self.assertEqual(summary.missing_financials, 1)
            self.assertEqual(summary.missing_profit_periods, 1)
            self.assertEqual(summary.insufficient_history, 1)
            self.assertEqual(summary.missing_financing_histories, 1)
            self.assertEqual(summary.missing_management_histories, 1)
            self.assertEqual(summary.missing_catalyst_histories, 1)
            unavailable = " ".join(summary.report.unavailable)
            self.assertIn("재무 미적재", unavailable)
            self.assertIn("61거래일 미만", unavailable)
            self.assertIn("회전율", unavailable)
            self.assertIn("경영진 위험", unavailable)
            self.assertIn("CB·BW·유상증자", unavailable)

    def test_pipeline_reports_incomplete_etf_specialist_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "069500", "KODEX 200", Market.KOSPI,
                AssetType.ETF, CompanyKind.FUND, date(2002, 10, 14),
            )])
            repository.save_bars(make_bars("069500"))
            repository.save_etf_snapshots([EtfSnapshot(
                "069500", date(2026, 9, 20), AS_OF - timedelta(hours=1),
                Decimal("42050"), Decimal("7800000000000"), Decimal("0.1"),
                None, None, None, "https://data.krx.co.kr/example",
            )])
            summary = CandidatePipeline(repository).run(as_of=AS_OF)
            self.assertEqual(summary.missing_etf_metrics, 1)
            self.assertEqual(summary.report.candidates[0].decision.value, "BUY_HOLD")
            self.assertIn("ETF 전용 지표 미적재·불완전", " ".join(summary.report.unavailable))


if __name__ == "__main__":
    unittest.main()
