from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.ingestion import DartFinancialEnricher, KrxHistoryIngestor
from stock_assistant.models import AssetType, CompanyKind, Evidence, FinancialSnapshot, Market, OHLCV, Security
from stock_assistant.providers.dart import DartCompanyProfile
from stock_assistant.providers.krx import KrxDailySnapshot
from stock_assistant.repository import StockRepository


UTC = timezone.utc


class FakeKrxClient:
    def __init__(self):
        self.snapshot_calls = 0

    def daily_snapshot(self, market, business_date, *, observed_at):
        self.snapshot_calls += 1
        if market == "KOSPI":
            symbol, name = "005930", "삼성전자"
        elif market == "ETF":
            symbol, name = "069500", "KODEX 200"
        else:
            return KrxDailySnapshot((), {})
        price = Decimal("100000")
        bar = OHLCV(
            symbol, business_date, price, price + 1000, price - 1000, price,
            100000, "KRX_OPEN_API", observed_at,
        )
        return KrxDailySnapshot((bar,), {symbol: name})

    def securities(self, market, business_date):
        if market == "KOSDAQ":
            return []
        return [Security(
            "005930", "삼성전자", Market.KOSPI,
            AssetType.COMMON, CompanyKind.UNKNOWN, date(1975, 6, 11),
        )]


class FakeDartClient:
    def corp_codes(self):
        return {"005930": "00126380"}

    def company_profile(self, corp_code):
        return DartCompanyProfile(CompanyKind.GENERAL, "26110", 12)

    def filings(self, **kwargs):
        return [Evidence(
            "DART", "삼성전자 - 사업보고서", "https://dart.fss.or.kr/example",
            datetime(2026, 3, 31, 14, 59, tzinfo=UTC),
            datetime(2026, 9, 21, tzinfo=UTC),
            True,
        )]

    def financial_statement(self, **kwargs):
        return FinancialSnapshot(
            kwargs["symbol"], kwargs["period_end"], kwargs["published_at"],
            Decimal("100"), Decimal("80"), Decimal("60"), (), (),
            "https://dart.fss.or.kr/example",
        )


class IngestionTests(unittest.TestCase):
    def test_backfill_persists_equity_etf_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            observed_at = datetime(2026, 9, 21, tzinfo=UTC)
            summary = KrxHistoryIngestor(
                repository, FakeKrxClient(), request_interval_seconds=0,
            ).backfill(
                end_date=date(2026, 9, 20),
                calendar_days=90,
                observed_at=observed_at,
            )
            self.assertGreaterEqual(summary.trading_days_with_data, 61)
            self.assertEqual(summary.securities_saved, 2)
            securities = repository.list_securities()
            self.assertEqual([item.symbol for item in securities], ["005930", "069500"])
            self.assertEqual(securities[1].asset_type, AssetType.ETF)
            self.assertIsNone(securities[1].listed_on)
            self.assertGreaterEqual(len(repository.bars_for("005930", as_of=observed_at)), 61)

    def test_backfill_bounds_request_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            ingestor = KrxHistoryIngestor(repository, FakeKrxClient(), request_interval_seconds=0)
            with self.assertRaisesRegex(ValueError, "between 90 and 366"):
                ingestor.backfill(
                    end_date=date(2026, 9, 20), calendar_days=10,
                    observed_at=datetime(2026, 9, 21, tzinfo=UTC),
                )

    def test_daily_ingestion_uses_only_one_market_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            observed_at = datetime(2026, 9, 21, tzinfo=UTC)
            summary = KrxHistoryIngestor(
                repository, FakeKrxClient(), request_interval_seconds=0,
            ).ingest_day(business_date=date(2026, 9, 18), observed_at=observed_at)
            self.assertEqual(summary.trading_days_with_data, 1)
            self.assertEqual(summary.bars_saved, 2)
            self.assertEqual(summary.securities_saved, 2)

    def test_daily_ingestion_skips_weekends_without_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            client = FakeKrxClient()
            summary = KrxHistoryIngestor(
                repository, client, request_interval_seconds=0,
            ).ingest_day(
                business_date=date(2026, 9, 20),
                observed_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
            self.assertEqual(summary.requested_weekdays, 0)
            self.assertEqual(summary.bars_saved, 0)
            self.assertEqual(client.snapshot_calls, 0)

    def test_dart_enrichment_maps_company_kind_and_persists_annual_financials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "005930", "삼성전자", Market.KOSPI,
                AssetType.COMMON, CompanyKind.UNKNOWN, date(1975, 6, 11),
            )])
            as_of = datetime(2026, 9, 21, tzinfo=UTC)
            summary = DartFinancialEnricher(
                repository, FakeDartClient(), request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=as_of, business_year=2025)
            self.assertEqual(summary.enriched, 1)
            self.assertEqual(repository.list_securities()[0].company_kind, CompanyKind.GENERAL)
            financial = repository.latest_financial("005930", as_of=as_of)
            self.assertIsNotNone(financial)
            assert financial is not None
            self.assertEqual(financial.period_end, date(2025, 12, 31))

            repeated = DartFinancialEnricher(
                repository, FakeDartClient(), request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=as_of, business_year=2025)
            self.assertEqual(repeated.enriched, 0)
            self.assertEqual(repeated.already_current, 1)


if __name__ == "__main__":
    unittest.main()
