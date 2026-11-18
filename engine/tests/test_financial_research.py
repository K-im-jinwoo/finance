from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from stock_assistant.financial_research import AnnualProfit, evaluate_financial
from stock_assistant.models import AssetType, CompanyKind, Evidence, Market, Security
from tests.test_screening import AS_OF, good_financial, samsung


UTC = timezone.utc


def history(symbol="005930"):
    return tuple(AnnualProfit(symbol, date(year, 12, 31), Decimal(str(100 + index * 10)),
                             datetime(year + 1, 3, 1, tzinfo=UTC), datetime(year + 1, 3, 2, tzinfo=UTC),
                             f"{year + 1}0000000001", "CFS", "https://dart.fss.or.kr/example")
                 for index, year in enumerate((2023, 2024, 2025)))


class FinancialResearchTests(unittest.TestCase):
    def evaluate(self, financial=None, **kwargs):
        return evaluate_financial(samsung(), financial or good_financial(), as_of=AS_OF, **kwargs)

    def test_financial_pass_needs_no_price_bars_and_does_not_reuse_old_price_score(self):
        result = self.evaluate()
        self.assertEqual(result.gate, "PASS")
        self.assertEqual(result.financial_score, Decimal("80"))
        self.assertEqual(result.score_breakdown["profit_history"]["points"], None)
        self.assertIn("THREE_YEAR_PROFIT_HISTORY_UNAVAILABLE", result.pending_checks)
        self.assertFalse(result.score_coverage["historical_replay_certified"])

    def test_three_year_verified_history_earns_points_and_canonical_order_reuses_identity(self):
        result = self.evaluate(annual_history=history())
        reordered = self.evaluate(annual_history=tuple(reversed(history())))
        self.assertEqual(result.financial_score, Decimal("100"))
        self.assertEqual(result.evaluation_id, reordered.evaluation_id)
        self.assertNotIn("THREE_YEAR_PROFIT_HISTORY_UNAVAILABLE", result.pending_checks)

    def test_optional_fcf_missing_remains_pass_without_redistributing_weights(self):
        result = self.evaluate(replace(good_financial(), free_cash_flow=None), annual_history=history())
        self.assertEqual(result.gate, "PASS")
        self.assertEqual(result.financial_score, Decimal("85"))
        self.assertIsNone(result.score_breakdown["free_cash_flow"]["points"])
        self.assertIn("FCF_UNAVAILABLE_OPTIONAL", result.advisories)
        self.assertNotIn("FCF_UNAVAILABLE_OPTIONAL", result.blocking_reasons)

    def test_missing_core_data_has_no_confirmed_score(self):
        missing = replace(good_financial(), ttm_operating_income=None, operating_cash_flow=None)
        result = self.evaluate(missing)
        self.assertEqual(result.gate, "VERIFY_PENDING")
        self.assertIsNone(result.financial_score)
        self.assertEqual(set(result.blocking_reasons), {"PROFIT_PERIODS_UNAVAILABLE", "OCF_UNAVAILABLE"})

    def test_no_snapshot_is_pending(self):
        result = evaluate_financial(samsung(), None, as_of=AS_OF)
        self.assertEqual(result.gate, "VERIFY_PENDING")
        self.assertIn("FINANCIAL_SNAPSHOT_UNAVAILABLE", result.pending_checks)

    def test_profit_and_ocf_zero_or_negative_are_excluded_even_with_other_missing_fields(self):
        for key in ("operating_income", "annual_operating_income", "ttm_operating_income", "operating_cash_flow"):
            for value in (Decimal("0"), Decimal("-1")):
                with self.subTest(key=key, value=value):
                    result = self.evaluate(replace(good_financial(), **{key: value, "ttm_source_url": None}))
                    self.assertEqual(result.gate, "EXCLUDED")
                    self.assertIsNone(result.financial_score)
                    self.assertEqual(result.pending_checks, ())

    def test_unknown_company_and_other_asset_policies_are_not_promoted(self):
        unknown = replace(samsung(), company_kind=CompanyKind.UNKNOWN)
        self.assertEqual(evaluate_financial(unknown, good_financial(), as_of=AS_OF).gate, "VERIFY_PENDING")
        bank = replace(samsung(), company_kind=CompanyKind.FINANCIAL)
        self.assertEqual(evaluate_financial(bank, good_financial(), as_of=AS_OF).gate, "NOT_APPLICABLE")
        etf = Security("069500", "ETF", Market.KOSPI, AssetType.ETF, CompanyKind.FUND, date(2000, 1, 1))
        self.assertEqual(evaluate_financial(etf, None, as_of=AS_OF).gate, "NOT_APPLICABLE")

    def test_negative_fcf_requires_a_specific_causal_fact_not_any_official_article(self):
        negative = replace(good_financial(), free_cash_flow=Decimal("-1"))
        unrelated = Evidence("DART", "계약", "https://dart.fss.or.kr/example", AS_OF, AS_OF, True, ("계약 체결",))
        result = self.evaluate(negative, fcf_growth_evidence=(unrelated,))
        self.assertIn("FCF_CAUSE_REVIEW_REQUIRED", result.pending_checks)
        self.assertEqual(result.financial_score, Decimal("65"))
        explained = replace(unrelated, facts=("FCF_NEGATIVE_DUE_TO_GROWTH_INVESTMENT", "ISSUER:005930",
                                              "FCF_PERIOD_END:2026-06-30"))
        result = self.evaluate(negative, fcf_growth_evidence=(explained,))
        self.assertEqual(result.financial_score, Decimal("75"))
        self.assertNotIn("FCF_CAUSE_REVIEW_REQUIRED", result.pending_checks)

    def test_growth_evidence_for_another_issuer_or_period_does_not_resolve_current_cause(self):
        negative = replace(good_financial(), free_cash_flow=Decimal("-1"))
        for issuer, period in (("000660", "2026-06-30"), ("005930", "2025-12-31")):
            evidence = Evidence("DART", "투자 지출", "https://dart.fss.or.kr/example", AS_OF, AS_OF, True,
                                ("FCF_NEGATIVE_DUE_TO_GROWTH_INVESTMENT", f"ISSUER:{issuer}", f"FCF_PERIOD_END:{period}"))
            self.assertIn("FCF_CAUSE_REVIEW_REQUIRED", self.evaluate(negative, fcf_growth_evidence=(evidence,)).pending_checks)

    def test_ttm_185_day_boundary_expires_without_new_source(self):
        financial = replace(good_financial(), period_end=date(2025, 12, 31), ttm_period_end=date(2025, 12, 31),
                            published_at=datetime(2026, 3, 1, tzinfo=UTC))
        boundary = datetime(2025, 12, 31, tzinfo=UTC) + timedelta(days=185)
        valid = evaluate_financial(samsung(), financial, as_of=boundary)
        stale = evaluate_financial(samsung(), financial, as_of=boundary + timedelta(days=1))
        self.assertEqual(valid.gate, "PASS")
        self.assertEqual(stale.gate, "VERIFY_PENDING")
        self.assertIsNone(stale.financial_score)
        self.assertIn("TTM_STALE", stale.blocking_reasons)
        self.assertEqual(valid.fingerprint, stale.fingerprint)
        self.assertNotEqual(valid.evaluation_id, stale.evaluation_id)

    def test_future_financial_history_evidence_and_naive_time_are_rejected(self):
        cases = (replace(good_financial(), published_at=AS_OF + timedelta(days=1)),
                 replace(good_financial(), period_end=date(2026, 10, 1)))
        for financial in cases:
            with self.assertRaises(ValueError):
                self.evaluate(financial)
        future = replace(history()[-1], observed_at=AS_OF + timedelta(days=1))
        with self.assertRaises(ValueError):
            self.evaluate(annual_history=history()[:-1] + (future,))
        with self.assertRaises(ValueError):
            evaluate_financial(samsung(), good_financial(), as_of=AS_OF.replace(tzinfo=None))

    def test_invalid_numbers_symbols_history_periods_and_bases_are_rejected(self):
        for value in (Decimal("NaN"), Decimal("Infinity")):
            with self.assertRaises(ValueError):
                self.evaluate(replace(good_financial(), operating_cash_flow=value))
        for items in (history("000660"), history()[:-1] + (replace(history()[-1], statement_basis="OFS"),),
                      (replace(history()[0], period_end=date(2022, 12, 31)),) + history()[1:]):
            with self.assertRaises(ValueError):
                self.evaluate(annual_history=items)
        with self.assertRaises(ValueError):
            self.evaluate(replace(good_financial(), symbol="000660"))

    def test_stale_history_is_not_awarded(self):
        old = tuple(replace(item, period_end=date(item.period_end.year - 3, 12, 31)) for item in history())
        result = self.evaluate(annual_history=old)
        self.assertEqual(result.financial_score, Decimal("80"))
        self.assertIn("ANNUAL_PROFIT_HISTORY_STALE", result.pending_checks)

    def test_conflicting_same_period_annual_inputs_do_not_produce_a_confirmed_score(self):
        financial = replace(good_financial(), period_end=date(2025, 12, 31))
        result = self.evaluate(financial, annual_history=history())
        self.assertEqual(result.gate, "VERIFY_PENDING")
        self.assertIsNone(result.financial_score)
        self.assertIn("ANNUAL_PROFIT_SOURCE_CONFLICT", result.blocking_reasons)

    def test_not_listed_and_delisted_security_are_excluded(self):
        for security in (replace(samsung(), listed_on=date(2026, 10, 1)),
                         replace(samsung(), delisted_on=AS_OF.date())):
            self.assertEqual(evaluate_financial(security, good_financial(), as_of=AS_OF).gate, "EXCLUDED")

