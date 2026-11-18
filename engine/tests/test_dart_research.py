from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
from pathlib import Path
import tempfile
import unittest

from stock_assistant.dart_research import DartResearchCollector, decode_bundle, receipt
from stock_assistant.models import CompanyKind, Evidence
from stock_assistant.providers.dart import DartCompanyProfile, DartFilingPage, DartOperatingIncomePeriods
from stock_assistant.providers.http import NetworkError, RateLimitError
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData
from stock_assistant.research_repository import ReadOnlyResearchRepository
from tests.test_screening import AS_OF, UTC, good_financial, samsung


def url(key):
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={key}"


class FakeDart:
    def __init__(self):
        self.calls = []
        self.filings = [Evidence("DART", f"사업보고서 ({year}.12)", url(f"{year + 1}0301000001"),
                                 AS_OF.replace(year=year + 1, month=3, day=1), AS_OF, True)
                        for year in (2023, 2024, 2025)]
        self.filings.append(Evidence("DART", "반기보고서 (2026.06)", url("20260814000001"),
                                     AS_OF.replace(month=8, day=14), AS_OF, True))
        self.no_cfs_interim = False

    def corp_codes(self):
        self.calls.append(("codes",))
        return {"005930": "00126380", "000001": "00126381"}

    def company_profile(self, corp_code):
        self.calls.append(("profile", corp_code))
        return DartCompanyProfile(CompanyKind.GENERAL, "26000", 12)

    def filing_page(self, **kw):
        self.calls.append(("filings", kw["page_no"]))
        first = date(int(kw["begin_date"][:4]), int(kw["begin_date"][4:6]), int(kw["begin_date"][6:]))
        last = date(int(kw["end_date"][:4]), int(kw["end_date"][4:6]), int(kw["end_date"][6:]))
        filings = tuple(f for f in self.filings if first <= f.published_at.date() <= last)
        return DartFilingPage(filings, {receipt(f.url): f.published_at for f in filings}, kw["page_no"], 1)

    def financial_statement(self, **kw):
        year = kw["business_year"]
        self.calls.append(("statement", year, kw["financial_statement_division"]))
        key = next(receipt(f.url) for f in reversed(self.filings) if f"({year}.12)" in f.title)
        income = Decimal(str(100 + (year - 2023) * 10))
        return replace(good_financial(), symbol=kw["symbol"], period_end=kw["period_end"], published_at=kw["published_at"],
                       operating_income=income, annual_operating_income=income, ttm_operating_income=income,
                       ttm_period_end=kw["period_end"], source_url=url(key), ttm_source_url=url(key))

    def operating_income_periods(self, **kw):
        self.calls.append(("interim", kw["financial_statement_division"]))
        if self.no_cfs_interim and kw["financial_statement_division"] == "CFS":
            return None
        return DartOperatingIncomePeriods(Decimal("80"), Decimal("60"), url("20260814000001"))


class DartResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.sqlite3"
        self.repo = StockRepository(self.source)
        self.repo.save_securities([replace(samsung(), company_kind=CompanyKind.UNKNOWN)])
        self.reader = ReadOnlyResearchRepository(self.source)
        self.store = ResearchData(Path(self.temp.name) / "research.sqlite3", source_database=self.source)
        self.client = FakeDart()

    def run_collection(self, client=None, now=AS_OF, **kwargs):
        return DartResearchCollector(self.reader, self.store, client or self.client, clock=lambda: now,
                                     sleeper=lambda _: None).run(**kwargs)

    def test_entire_common_universe_classification_three_years_and_ttm_without_price_filter(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.run_collection()
        self.assertEqual(result["results"][0]["status"], "COLLECTED")
        record = self.store.latest("financial_bundle", "005930", as_of=AS_OF)
        security, snapshot, history = decode_bundle(record["payload"])
        self.assertEqual(security.company_kind, CompanyKind.GENERAL)
        self.assertEqual([p.period_end.year for p in history], [2023, 2024, 2025])
        self.assertTrue(all(p.observed_at == AS_OF for p in history))
        self.assertEqual(snapshot.ttm_operating_income, Decimal("140"))
        self.assertEqual(snapshot.annual_operating_income, Decimal("120"))
        self.assertEqual(snapshot.ttm_period_end, date(2026, 6, 30))
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_same_day_no_requests_then_new_day_only_incremental_filings_and_cached_statements(self):
        self.run_collection()
        self.client.calls.clear()
        self.assertEqual(self.run_collection()["requests"], 0)
        self.assertEqual(self.client.calls, [])
        self.run_collection(now=AS_OF + timedelta(days=1))
        self.assertEqual(self.client.calls, [("codes",), ("filings", 1)])
        record = self.store.latest("financial_bundle", "005930", as_of=AS_OF + timedelta(days=1))
        self.assertEqual(record["observed_at"], AS_OF.isoformat())
        self.assertEqual(len(self.store.records("dart_filing", "005930", as_of=AS_OF + timedelta(days=1))), 4)
        cursor = self.store.latest("dart_cursor", "005930", as_of=AS_OF + timedelta(days=1))["payload"]
        self.assertNotIn("filings", cursor)
        self.assertEqual(cursor["filing_count"], 4)

    def test_ofs_fallback_restarts_full_lineage_without_mixing(self):
        self.client.no_cfs_interim = True
        self.run_collection()
        record = self.store.latest("financial_bundle", "005930", as_of=AS_OF)
        self.assertEqual(record["payload"]["statement_basis"], "OFS")
        self.assertEqual({p["statement_basis"] for p in record["payload"]["annual_history"]}, {"OFS"})
        self.assertEqual(len([c for c in self.client.calls if c[0] == "statement"]), 6)

    def test_new_correction_receipt_refreshes_only_changed_annual_input(self):
        self.run_collection()
        self.client.calls.clear()
        self.client.filings.append(Evidence("DART", "[정정]사업보고서 (2025.12)", url("20260920000001"),
                                             AS_OF, AS_OF, True))
        self.run_collection(now=AS_OF + timedelta(days=1))
        statements = [c for c in self.client.calls if c[0] == "statement"]
        self.assertEqual(statements, [("statement", 2025, "CFS")])
        record = self.store.latest("financial_bundle", "005930", as_of=AS_OF + timedelta(days=1))
        self.assertEqual(record["payload"]["annual_history"][-1]["receipt_id"], "20260920000001")

    def test_failed_page_never_saves_coverage_or_success_bundle(self):
        original = self.client.filing_page
        def pages(**kwargs):
            if kwargs["page_no"] == 2:
                raise NetworkError("temporary")
            return replace(original(**kwargs), total_page=3)
        self.client.filing_page = pages
        result = self.run_collection()
        self.assertEqual(result["results"][0]["error_code"], "NETWORK_ERROR")
        self.assertIsNone(self.store.latest("dart_cursor", "005930", as_of=AS_OF))
        self.assertIsNone(self.store.latest("financial_bundle", "005930", as_of=AS_OF))

    def test_over_limit_windows_split_and_exactly_fifty_pages_do_not_split(self):
        calls = []
        def pages(**kw):
            calls.append(kw)
            return DartFilingPage((), {}, kw["page_no"], 51 if kw["begin_date"] != kw["end_date"] else 1)
        self.client.filing_page = pages
        collector = DartResearchCollector(self.reader, self.store, self.client, clock=lambda: AS_OF, sleeper=lambda _: None)
        collector._filings("00126380", date(2026, 1, 1), date(2026, 1, 2), AS_OF)
        self.assertEqual(len(calls), 3)
        calls.clear()
        def fifty(**kw):
            calls.append(kw)
            return DartFilingPage((), {}, kw["page_no"], 50)
        self.client.filing_page = fifty
        collector._filings("00126380", date(2026, 1, 1), date(2026, 1, 2), AS_OF)
        self.assertEqual(len(calls), 50)

    def test_rate_limit_halts_batch_and_budgets_remain_bounded(self):
        self.repo.save_securities([replace(samsung(), symbol="000001")])
        self.client.company_profile = lambda **_: (_ for _ in ()).throw(RateLimitError("limit"))
        result = self.run_collection()
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["results"][0]["error_code"], "RATE_LIMITED")
        result = DartResearchCollector(self.reader, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None,
                                       request_limit=1).run()
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["results"][0]["error_code"], "DART_REQUEST_BUDGET_EXHAUSTED")

    def test_bad_statement_receipt_does_not_advance_cursor(self):
        method = self.client.financial_statement
        self.client.financial_statement = lambda **kw: replace(method(**kw), source_url=url("20260101000000"))
        result = self.run_collection()
        self.assertEqual(result["results"][0]["error_code"], "DART_STATEMENT_RECEIPT_MISMATCH")
        self.assertIsNone(self.store.latest("dart_cursor", "005930", as_of=AS_OF))

    def test_non_december_company_requires_separate_period_policy(self):
        self.client.company_profile = lambda **_: DartCompanyProfile(CompanyKind.GENERAL, "26000", 3)
        result = self.run_collection()
        self.assertEqual(result["results"][0]["status"], "SEPARATE_POLICY_REQUIRED")
        self.assertFalse(any(c[0] == "statement" for c in self.client.calls))

    def test_source_same_path_and_foreign_database_are_rejected(self):
        with self.assertRaises(ValueError):
            ResearchData(self.source, source_database=self.source)
        other = Path(self.temp.name) / "foreign.sqlite3"
        StockRepository(other)
        with self.assertRaises(ValueError):
            ResearchData(other, source_database=self.source)

    def test_news_review_target_is_updated_inside_the_existing_batch_with_cached_statements(self):
        self.run_collection()
        self.repo.save_securities([replace(samsung(),symbol='000001')])
        self.store.put('archive_cursor','005930',{'status':'PARTIAL'},observed_at=AS_OF)
        self.client.calls.clear()
        result=self.run_collection(now=AS_OF+timedelta(days=1),batch_size=2)
        self.assertEqual([r['symbol'] for r in result['results']],['005930','000001'])
        self.assertEqual(len([c for c in self.client.calls if c[0]=='statement']),3)
        self.assertEqual(result['selected'],2)


if __name__ == "__main__":
    unittest.main()
