from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from stock_assistant.ingestion import DartDisclosureEnricher, DartFinancialEnricher, KrxHistoryIngestor
from stock_assistant.models import AssetType, CompanyKind, EtfSnapshot, Evidence, FinancialSnapshot, FinancingEvent, Market, OHLCV, Security
from stock_assistant.providers.dart import DartCompanyProfile, DartFilingPage, DartOperatingIncomePeriods
from stock_assistant.providers.http import RateLimitError
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
        etf_snapshots = ()
        if market == "ETF":
            etf_snapshots = (EtfSnapshot(
                symbol, business_date, observed_at, Decimal("99900"),
                Decimal("100000000000"), Decimal("0.1"), None, None, None,
                "https://data.krx.co.kr/example",
            ),)
        return KrxDailySnapshot((bar,), {symbol: name}, etf_snapshots)

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

    def filing_page(self, **kwargs):
        published_at = datetime(2025, 9, 18, 14, 59, 59, tzinfo=UTC)
        filings = (
            Evidence(
                "DART", "삼성전자 - 단일판매ㆍ공급계약체결",
                "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20250918000001",
                published_at, kwargs["observed_at"], True,
            ),
            Evidence(
                "DART", "삼성전자 - 횡령ㆍ배임혐의발생",
                "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20250918000002",
                published_at, kwargs["observed_at"], True,
            ),
        )
        return DartFilingPage(
            filings,
            {"20250918000001": published_at, "20250918000002": published_at},
            kwargs["page_no"], 1,
        )

    def financial_statement(self, **kwargs):
        return FinancialSnapshot(
            kwargs["symbol"], kwargs["period_end"], kwargs["published_at"],
            Decimal("100"), Decimal("80"), Decimal("60"), (), (),
            "https://dart.fss.or.kr/example",
            annual_operating_income=Decimal("100"),
            ttm_operating_income=Decimal("100"),
            ttm_period_end=kwargs["period_end"],
            ttm_source_url="https://dart.fss.or.kr/example",
        )

    def financing_events(self, **kwargs):
        if kwargs["event_type"] != "CB":
            return []
        return [FinancingEvent(
            kwargs["symbol"], "CB", datetime(2025, 9, 18, tzinfo=UTC),
            True, True, "https://dart.fss.or.kr/cb", Decimal("12.5"),
            "운영자금=100", True,
        )]


class FakeFinancialDartClient(FakeDartClient):
    def company_profile(self, corp_code):
        return DartCompanyProfile(CompanyKind.FINANCIAL, "64992", 12)


class FakeInterimDartClient(FakeDartClient):
    def filings(self, **kwargs):
        return super().filings(**kwargs) + [Evidence(
            "DART", "삼성전자 - 반기보고서 (2026.06)", "https://dart.fss.or.kr/interim",
            datetime(2026, 8, 14, 14, 59, tzinfo=UTC),
            datetime(2026, 9, 21, tzinfo=UTC), True,
        )]

    def operating_income_periods(self, **kwargs):
        return DartOperatingIncomePeriods(
            Decimal("50"), Decimal("20"), "https://dart.fss.or.kr/interim",
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
            self.assertIsNotNone(repository.latest_etf_snapshot("069500", as_of=observed_at))

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
            self.assertEqual(repeated.enriched, 1)
            self.assertEqual(repeated.already_current, 0)

    def test_dart_enrichment_does_not_apply_general_cashflow_contract_to_financial_company(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "005930", "테스트금융", Market.KOSPI,
                AssetType.COMMON, CompanyKind.UNKNOWN, date(1975, 6, 11),
            )])
            as_of = datetime(2026, 9, 21, tzinfo=UTC)
            summary = DartFinancialEnricher(
                repository, FakeFinancialDartClient(), request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=as_of, business_year=2025)
            self.assertEqual(summary.enriched, 0)
            self.assertEqual(summary.requires_specialist_metrics, 1)
            self.assertIsNone(repository.latest_financial("005930", as_of=as_of))
            self.assertEqual(repository.list_securities()[0].company_kind, CompanyKind.FINANCIAL)

    def test_dart_enrichment_combines_annual_and_interim_for_ttm_profit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "005930", "삼성전자", Market.KOSPI,
                AssetType.COMMON, CompanyKind.UNKNOWN, date(1975, 6, 11),
            )])
            as_of = datetime(2026, 9, 21, tzinfo=UTC)
            summary = DartFinancialEnricher(
                repository, FakeInterimDartClient(), request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=as_of, business_year=2025)
            self.assertEqual(summary.enriched, 1)
            snapshot = repository.latest_financial("005930", as_of=as_of)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.annual_operating_income, Decimal("100"))
            self.assertEqual(snapshot.ttm_operating_income, Decimal("130"))
            self.assertEqual(snapshot.ttm_period_end, date(2026, 6, 30))
            self.assertEqual(snapshot.ttm_source_url, "https://dart.fss.or.kr/interim")

    def test_dart_disclosure_enrichment_marks_zero_safe_coverage_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "005930", "삼성전자", Market.KOSPI,
                AssetType.COMMON, CompanyKind.GENERAL, date(1975, 6, 11),
            )])
            as_of = datetime(2026, 9, 21, tzinfo=UTC)
            observed_at = as_of + timedelta(hours=1)
            summary = DartDisclosureEnricher(
                repository, FakeDartClient(), request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=as_of, observed_at=observed_at)
            self.assertEqual(summary.covered, 1)
            self.assertEqual(summary.events_saved, 1)
            self.assertEqual(summary.catalysts_saved, 1)
            self.assertEqual(summary.management_risks_saved, 1)
            events = repository.financing_events_for(
                "005930", as_of=as_of,
                since=datetime(2021, 9, 17, tzinfo=UTC),
            )
            self.assertEqual(events[0].event_type, "CB")
            self.assertFalse(repository.has_coverage(
                symbol="005930", dataset="DART_FINANCING",
                required_start_date="2021-09-20", required_end_date="2026-09-20",
                as_of=as_of,
            ))
            self.assertTrue(repository.has_coverage(
                symbol="005930", dataset="DART_FINANCING",
                required_start_date="2021-09-20", required_end_date="2026-09-20",
                as_of=observed_at,
            ))
            self.assertEqual(repository.catalysts_for(
                "005930", as_of=as_of,
                since=datetime(2025, 1, 1, tzinfo=UTC),
            ), [])
            self.assertEqual(len(repository.catalysts_for(
                "005930", as_of=observed_at,
                since=datetime(2025, 1, 1, tzinfo=UTC),
            )), 1)
            self.assertFalse(repository.management_risks_for(
                "005930", as_of=observed_at,
                since=datetime(2016, 1, 1, tzinfo=UTC),
            )[0].confirmed)


class DartDisclosurePaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observed_at = datetime(2026, 10, 6, tzinfo=UTC)
        self.client = FakeDartClient()
        self.client.filing_page = Mock()

    def collect(self, start_date: date, end_date: date, *, sleeper=None):
        enricher = DartDisclosureEnricher(
            None, self.client, request_interval_seconds=0.2 if sleeper else 0,
            sleeper=sleeper or Mock(),
        )
        return enricher._receipt_dates(
            corp_code="00126380", start_date=start_date, end_date=end_date,
            observed_at=self.observed_at,
        )

    def filing(self, day: date, index: int = 1) -> Evidence:
        receipt = f"{day:%Y%m%d}{index:06d}"
        return Evidence(
            "DART", "삼성전자 - 단일판매ㆍ공급계약체결",
            f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
            datetime(day.year, day.month, day.day, 14, 59, 59, tzinfo=UTC),
            self.observed_at, True,
        )

    def test_large_history_splits_without_gaps_and_paginates_each_half(self) -> None:
        def fetch(**query):
            begin = query["begin_date"]
            end = query["end_date"]
            page_no = query["page_no"]
            if (begin, end) == ("20260901", "20260904"):
                return DartFilingPage((), {}, page_no, 51)
            day = datetime.strptime(begin if page_no == 1 else end, "%Y%m%d").date()
            filing = self.filing(day)
            receipt = filing.url.rsplit("=", 1)[-1]
            return DartFilingPage((filing,), {receipt: filing.published_at}, page_no, 2)

        self.client.filing_page.side_effect = fetch
        sleeper = Mock()
        receipts, filings = self.collect(date(2026, 9, 1), date(2026, 9, 4), sleeper=sleeper)
        self.assertEqual(len(receipts), 4)
        self.assertEqual(len(filings), 4)
        calls = [call.kwargs for call in self.client.filing_page.call_args_list]
        self.assertEqual([(q["begin_date"], q["end_date"], q["page_no"]) for q in calls], [
            ("20260901", "20260904", 1),
            ("20260901", "20260902", 1), ("20260901", "20260902", 2),
            ("20260903", "20260904", 1), ("20260903", "20260904", 2),
        ])
        self.assertTrue(all(q["page_count"] == 100 and q["corp_code"] == "00126380"
                            and q["observed_at"] == self.observed_at for q in calls))
        self.assertEqual(sleeper.call_count, 5)
        sleeper.assert_called_with(0.2)

    def test_dense_history_splits_again_until_each_window_fits(self) -> None:
        def fetch(**query):
            begin = datetime.strptime(query["begin_date"], "%Y%m%d").date()
            end = datetime.strptime(query["end_date"], "%Y%m%d").date()
            if (end - begin).days > 1:
                return DartFilingPage((), {}, query["page_no"], 51)
            return DartFilingPage((), {}, 0, 0)

        self.client.filing_page.side_effect = fetch
        self.assertEqual(self.collect(date(2026, 9, 1), date(2026, 9, 8)), ({}, []))
        self.assertEqual(self.client.filing_page.call_count, 7)
        calls = [call.kwargs for call in self.client.filing_page.call_args_list]
        leaves = [(q["begin_date"], q["end_date"]) for q in calls
                  if int(q["end_date"]) - int(q["begin_date"]) == 1]
        self.assertEqual(leaves, [("20260901", "20260902"), ("20260903", "20260904"),
                                 ("20260905", "20260906"), ("20260907", "20260908")])

    def test_exactly_50_pages_keeps_original_range_and_collects_last_page(self) -> None:
        def fetch(**query):
            filing = self.filing(date(2026, 9, 1), query["page_no"])
            receipt = filing.url.rsplit("=", 1)[-1]
            return DartFilingPage((filing,), {receipt: filing.published_at}, query["page_no"], 50)

        self.client.filing_page.side_effect = fetch
        receipts, filings = self.collect(date(2026, 9, 1), date(2026, 9, 4))
        self.assertEqual(len(receipts), 50)
        self.assertEqual(len(filings), 50)
        self.assertIn("20260901000050", receipts)
        self.assertEqual(self.client.filing_page.call_count, 50)
        self.assertTrue(all(call.kwargs["begin_date"] == "20260901"
                            and call.kwargs["end_date"] == "20260904"
                            for call in self.client.filing_page.call_args_list))

    def test_empty_history_finishes_after_one_request(self) -> None:
        self.client.filing_page.return_value = DartFilingPage((), {}, 0, 0)
        self.assertEqual(self.collect(date(2026, 9, 1), date(2026, 9, 4)), ({}, []))
        self.client.filing_page.assert_called_once()

    def test_single_day_overflow_fails_closed_with_context(self) -> None:
        self.client.filing_page.return_value = DartFilingPage((), {}, 1, 51)
        with self.assertRaisesRegex(ValueError, "single day.*00126380.*2026-09-01.*51"):
            self.collect(date(2026, 9, 1), date(2026, 9, 1))
        self.client.filing_page.assert_called_once()

    def test_bad_page_number_and_changing_page_count_fail_closed(self) -> None:
        for pages, message in (
            ([DartFilingPage((), {}, 2, 2)], "page number"),
            ([DartFilingPage((), {}, 1, 2), DartFilingPage((), {}, 2, 3)], "page count changed"),
            ([DartFilingPage((), {}, 1, 2), DartFilingPage((), {}, 0, 0)], "page count changed"),
        ):
            with self.subTest(message=message, pages=pages):
                self.client.filing_page.side_effect = pages
                with self.assertRaisesRegex(ValueError, message):
                    self.collect(date(2026, 9, 1), date(2026, 9, 4))

    def test_conflicting_receipt_dates_across_pages_still_fail_closed(self) -> None:
        receipt = "20260901000001"
        self.client.filing_page.side_effect = [
            DartFilingPage((), {receipt: self.observed_at}, 1, 2),
            DartFilingPage((), {receipt: self.observed_at - timedelta(days=1)}, 2, 2),
        ]
        with self.assertRaisesRegex(ValueError, "receipt date changed"):
            self.collect(date(2026, 9, 1), date(2026, 9, 4))

    def test_invalid_date_range_is_rejected_before_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "start_date"):
            self.collect(date(2026, 9, 4), date(2026, 9, 1))
        self.client.filing_page.assert_not_called()

    def test_successful_split_persists_full_history_and_original_coverage(self) -> None:
        end_date = date(2026, 10, 5)
        management_start = end_date - timedelta(days=365 * 10 + 3)
        financing_start = end_date - timedelta(days=365 * 5 + 2)
        old_risk = self.filing(management_start)
        old_risk = Evidence(
            old_risk.source_type, "삼성전자 - 횡령ㆍ배임혐의발생", old_risk.url,
            old_risk.published_at, old_risk.observed_at, old_risk.official,
        )

        def fetch(**query):
            begin = datetime.strptime(query["begin_date"], "%Y%m%d").date()
            end = datetime.strptime(query["end_date"], "%Y%m%d").date()
            if begin == management_start and end == end_date:
                return DartFilingPage((), {}, 1, 51)
            recent = FakeDartClient().filing_page(**query).filings
            filings = tuple(filing for filing in (old_risk, *recent)
                            if begin <= filing.published_at.date() <= end)
            receipts = {filing.url.rsplit("=", 1)[-1]: filing.published_at for filing in filings}
            return DartFilingPage(filings, receipts, 1, 1)

        self.client.filing_page.side_effect = fetch
        self.client.financing_events = Mock(wraps=self.client.financing_events)
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.save_securities([Security(
                "005930", "삼성전자", Market.KOSPI,
                AssetType.COMMON, CompanyKind.GENERAL, date(1975, 6, 11),
            )])
            summary = DartDisclosureEnricher(
                repository, self.client, request_interval_seconds=0,
            ).enrich(symbols=["005930"], as_of=self.observed_at)
            self.assertEqual(summary.covered, 1)
            self.assertEqual(summary.events_saved, 1)
            self.assertEqual(summary.catalysts_saved, 1)
            self.assertEqual(summary.management_risks_saved, 2)
            self.assertEqual(self.client.filing_page.call_count, 3)
            for call in self.client.financing_events.call_args_list:
                self.assertEqual(call.kwargs["begin_date"], financing_start.strftime("%Y%m%d"))
                self.assertEqual(call.kwargs["end_date"], "20261005")
                self.assertEqual(len(call.kwargs["receipt_dates"]), 3)
            for dataset, start in (("DART_FINANCING", financing_start),
                                   ("DART_CATALYST", management_start),
                                   ("DART_MANAGEMENT_RISK", management_start)):
                self.assertTrue(repository.has_coverage(
                    symbol="005930", dataset=dataset,
                    required_start_date=start.isoformat(), required_end_date=end_date.isoformat(),
                    as_of=self.observed_at,
                ))
            risks = repository.management_risks_for(
                "005930", as_of=self.observed_at, since=datetime(2015, 1, 1, tzinfo=UTC),
            )
            self.assertIn(old_risk.url, [evidence.url for risk in risks for evidence in risk.evidence])

    def test_partial_split_failure_does_not_record_coverage_or_events(self) -> None:
        for failure in (RateLimitError("DART request limit exceeded"), "request_budget"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                repository = StockRepository(Path(directory) / "stock.sqlite3")
                repository.save_securities([Security(
                    "005930", "삼성전자", Market.KOSPI,
                    AssetType.COMMON, CompanyKind.GENERAL, date(1975, 6, 11),
                )])
                self.client.filing_page.reset_mock()
                self.client.filing_page.side_effect = [
                    DartFilingPage((), {}, 1, 51), DartFilingPage((), {}, 0, 0),
                    failure if isinstance(failure, Exception) else DartFilingPage((), {}, 0, 0),
                ]
                enricher = DartDisclosureEnricher(repository, self.client, request_interval_seconds=0)
                limit = 2 if failure == "request_budget" else 500
                error_type = ValueError if failure == "request_budget" else RateLimitError
                with patch.object(DartDisclosureEnricher, "_MAX_FILING_REQUESTS", limit):
                    with self.assertRaises(error_type):
                        enricher.enrich(symbols=["005930"], as_of=self.observed_at)
                self.assertEqual(self.client.filing_page.call_count, limit if limit == 2 else 3)
                for dataset in ("DART_FINANCING", "DART_CATALYST", "DART_MANAGEMENT_RISK"):
                    self.assertFalse(repository.has_coverage(
                        symbol="005930", dataset=dataset,
                        required_start_date="2026-10-01", required_end_date="2026-10-05",
                        as_of=self.observed_at,
                    ))
                self.assertEqual(repository.financing_events_for(
                    "005930", as_of=self.observed_at, since=datetime(2015, 1, 1, tzinfo=UTC),
                ), [])


if __name__ == "__main__":
    unittest.main()
