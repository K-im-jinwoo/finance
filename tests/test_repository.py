from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.models import Decision, Holding, ScreeningResult
from stock_assistant.models import (
    AssetType, Catalyst, CatalystStatus, CompanyKind, EtfSnapshot, Evidence,
    FinancialCompanySnapshot, FinancialSnapshot,
    FinancingEvent, ManagementRisk, Market, OHLCV, Security,
)
from stock_assistant.repository import StockRepository
from tests.helpers import make_bars


UTC = timezone.utc


class RepositoryTests(unittest.TestCase):
    def test_holding_upsert_is_idempotent_and_preserves_broker_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.replace_holding(Holding("미래에셋", "007660", Decimal("10"), Decimal("130000")))
            repository.replace_holding(Holding("미래에셋", "007660", Decimal("20"), Decimal("102000")))
            repository.replace_holding(Holding("토스증권", "007660", Decimal("1"), Decimal("100000")))
            holdings = repository.list_holdings()
            self.assertEqual(len(holdings), 2)
            self.assertEqual(holdings[0].quantity, Decimal("20"))

    def test_screening_result_upsert_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            result = ScreeningResult(
                "005930", datetime(2026, 9, 21, tzinfo=UTC), Decision.BUY_HOLD,
                Decimal("50"), (), (), ("확인",), {},
            )
            repository.save_screening_results("R-1", [result])
            repository.save_screening_results("R-1", [result])
            with repository._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM screening_results").fetchone()[0]
            self.assertEqual(count, 1)

    def test_full_report_is_idempotent_and_retrievable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            report_id = "R-20260921T0000Z-ABCDEF12"
            payload = {"report_id": report_id, "candidates": [], "fact_summary": ["공식 공시"]}
            repository.save_report(report_id, "2026-09-21T00:00:00+00:00", payload)
            repository.save_report(report_id, "2026-09-21T00:00:00+00:00", payload)
            self.assertEqual(repository.get_report(report_id), payload)
            with self.assertRaisesRegex(ValueError, "different content"):
                repository.save_report(
                    report_id,
                    "2026-09-21T00:00:00+00:00",
                    {"report_id": report_id, "candidates": ["changed"]},
                )

    def test_normalized_market_and_financial_data_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            security = Security(
                "005930", "삼성전자", Market.KOSPI,
                AssetType.COMMON, CompanyKind.GENERAL, datetime(1975, 6, 11).date(),
            )
            repository.save_securities([security])
            repository.save_bars(make_bars())
            snapshot = FinancialSnapshot(
                "005930", datetime(2025, 12, 31).date(), datetime(2026, 3, 20, tzinfo=UTC),
                Decimal("100"), Decimal("80"), Decimal("60"),
                (Decimal("8"), Decimal("8.1")), (), "https://dart.fss.or.kr/example",
            )
            repository.save_financial_snapshot(snapshot)
            as_of = datetime(2026, 9, 20, tzinfo=UTC)
            self.assertEqual(repository.list_securities(), [security])
            self.assertEqual(len(repository.bars_for("005930", as_of=as_of)), 80)
            self.assertEqual(repository.latest_financial("005930", as_of=as_of), snapshot)

    def test_etf_and_financial_company_snapshots_are_point_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            observed = datetime(2026, 9, 18, 15, tzinfo=UTC)
            etf = EtfSnapshot(
                "069500", date(2026, 9, 18), observed,
                Decimal("42050"), Decimal("7800000000000"), Decimal("0.1"),
                None, None, None, "https://data.krx.co.kr/example",
            )
            financial_company = FinancialCompanySnapshot(
                "105560", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
                Decimal("14"), Decimal("9"), Decimal("0.7"), Decimal("0.5"),
                Decimal("180"), "배당", "https://fss.or.kr/example",
            )
            repository.save_etf_snapshots([etf])
            repository.save_financial_company_snapshot(financial_company)
            self.assertIsNone(repository.latest_etf_snapshot(
                "069500", as_of=datetime(2026, 9, 18, 14, tzinfo=UTC),
            ))
            self.assertEqual(repository.latest_etf_snapshot(
                "069500", as_of=datetime(2026, 9, 18, 16, tzinfo=UTC),
            ), etf)
            self.assertEqual(repository.latest_financial_company(
                "105560", as_of=datetime(2026, 9, 20, tzinfo=UTC),
            ), financial_company)

    def test_market_history_keeps_revisions_and_respects_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            early = OHLCV(
                "005930", date(2026, 9, 18), Decimal("100"), Decimal("110"),
                Decimal("90"), Decimal("100"), 1000, "KRX_OPEN_API",
                datetime(2026, 9, 18, 9, tzinfo=UTC),
            )
            revised = OHLCV(
                "005930", date(2026, 9, 18), Decimal("100"), Decimal("110"),
                Decimal("90"), Decimal("105"), 1200, "KRX_OPEN_API",
                datetime(2026, 9, 19, 9, tzinfo=UTC),
            )
            repository.save_bars([early, revised])
            self.assertEqual(
                repository.bars_for(
                    "005930", as_of=datetime(2026, 9, 18, 12, tzinfo=UTC),
                )[0].close,
                Decimal("100"),
            )
            self.assertEqual(
                repository.bars_for(
                    "005930", as_of=datetime(2026, 9, 20, 12, tzinfo=UTC),
                )[0].close,
                Decimal("105"),
            )
            self.assertEqual(
                repository.bars_since(
                    "005930", start_date=date(2026, 9, 18),
                    as_of=datetime(2026, 9, 18, 12, tzinfo=UTC),
                )[0].close,
                Decimal("100"),
            )

    def test_financing_history_and_coverage_are_point_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            event = FinancingEvent(
                "005930", "CB", datetime(2025, 9, 18, tzinfo=UTC), True, True,
                "https://dart.fss.or.kr/example", Decimal("12.5"), "운영자금=100", True,
            )
            repository.save_financing_events([event])
            repository.save_coverage(
                symbol="005930", dataset="DART_FINANCING",
                start_date="2021-09-17", end_date="2026-09-17",
                observed_at=datetime(2026, 9, 18, tzinfo=UTC),
            )
            self.assertEqual(repository.financing_events_for(
                "005930", as_of=datetime(2026, 9, 18, tzinfo=UTC),
                since=datetime(2021, 9, 17, tzinfo=UTC),
            ), [event])
            self.assertFalse(repository.has_coverage(
                symbol="005930", dataset="DART_FINANCING",
                required_start_date="2021-09-17", required_end_date="2026-09-17",
                as_of=datetime(2026, 9, 17, tzinfo=UTC),
            ))
            self.assertTrue(repository.has_coverage(
                symbol="005930", dataset="DART_FINANCING",
                required_start_date="2021-09-17", required_end_date="2026-09-17",
                as_of=datetime(2026, 9, 18, tzinfo=UTC),
            ))

    def test_catalyst_and_management_evidence_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            published = datetime(2026, 9, 18, 14, 59, 59, tzinfo=UTC)
            evidence = Evidence(
                "DART", "회사 - 단일판매ㆍ공급계약체결",
                "https://dart.fss.or.kr/contract", published,
                datetime(2026, 9, 19, tzinfo=UTC), True,
            )
            catalyst = Catalyst(
                "005930", "CONTRACT", CatalystStatus.PARTIAL,
                published, None, (evidence,),
            )
            risk = ManagementRisk(
                "005930", False, "OFFICIAL_REVIEW", (evidence,),
            )
            repository.save_catalysts([catalyst])
            repository.save_management_risks([risk])
            as_of = datetime(2026, 9, 20, tzinfo=UTC)
            since = datetime(2025, 9, 20, tzinfo=UTC)
            self.assertEqual(repository.catalysts_for("005930", as_of=as_of, since=since), [catalyst])
            self.assertEqual(repository.management_risks_for("005930", as_of=as_of, since=since), [risk])


if __name__ == "__main__":
    unittest.main()
