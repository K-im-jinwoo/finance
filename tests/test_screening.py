from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_assistant.models import (
    AssetType,
    Catalyst,
    CatalystStatus,
    CompanyKind,
    Decision,
    EtfSnapshot,
    Evidence,
    FinancialCompanySnapshot,
    FinancialSnapshot,
    FinancingEvent,
    ManagementRisk,
    Market,
    Security,
    StrategyType,
    HoldingPeriod,
)
from stock_assistant.screening import screen_security, select_top_candidates
from stock_assistant.validation import ContractError
from tests.helpers import make_bars


UTC = timezone.utc
AS_OF = datetime(2026, 9, 20, 12, tzinfo=UTC)


def samsung() -> Security:
    return Security("005930", "삼성전자", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(1975, 6, 11))


def good_financial() -> FinancialSnapshot:
    return FinancialSnapshot(
        "005930", date(2026, 6, 30), datetime(2026, 8, 14, tzinfo=UTC),
        Decimal("100"), Decimal("80"), Decimal("60"),
        (Decimal("8"), Decimal("8.1")), (Decimal("6"), Decimal("6.2")),
        "https://dart.fss.or.kr/example",
        annual_operating_income=Decimal("100"),
        ttm_operating_income=Decimal("120"),
        ttm_period_end=date(2026, 6, 30),
        ttm_source_url="https://dart.fss.or.kr/ttm",
    )


def confirmed_catalyst() -> Catalyst:
    evidence = Evidence(
        "DART", "공식 계약 공시", "https://dart.fss.or.kr/example",
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 1, 1, tzinfo=UTC), True,
        ("계약 체결",),
    )
    return Catalyst(
        "005930", "CONTRACT", CatalystStatus.CONFIRMED,
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC), (evidence,),
    )


class ScreeningTests(unittest.TestCase):
    def test_good_common_stock_becomes_candidate_with_reason_codes(self) -> None:
        result = screen_security(
            samsung(), make_bars(), as_of=AS_OF,
            financial=good_financial(), catalysts=(confirmed_catalyst(),),
        )
        self.assertEqual(result.decision, Decision.CANDIDATE)
        self.assertIn("MOMENTUM_CONTINUATION", result.reasons)
        self.assertIn("OCF_POSITIVE", result.reasons)
        self.assertIn("CATALYST_CONFIRMED", result.reasons)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.market, "KOSPI")
        self.assertEqual(result.asset_type, "COMMON")
        self.assertEqual(result.strategy, StrategyType.MOMENTUM_CONTINUATION)
        self.assertEqual(result.expected_holding_period, HoldingPeriod.SEVERAL_DAYS)
        self.assertIn("CONTRACT:CONFIRMED:2026-09-01", result.catalyst_states)
        self.assertIn("https://dart.fss.or.kr/example", result.evidence_urls)
        self.assertTrue(result.invalidation_conditions)

    def test_operating_loss_or_confirmed_management_risk_excludes(self) -> None:
        bad_financial = FinancialSnapshot(
            "005930", date(2026, 6, 30), datetime(2026, 8, 14, tzinfo=UTC),
            Decimal("-1"), Decimal("1"), Decimal("1"), (), (), "https://dart.fss.or.kr/example",
        )
        risk_evidence = Evidence(
            "COURT", "확정 판결", "https://example.test/court",
            datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC), True,
        )
        result = screen_security(
            samsung(), make_bars(), as_of=AS_OF, financial=bad_financial,
            management_risks=(ManagementRisk("005930", True, "EMBEZZLEMENT", (risk_evidence,)),),
        )
        self.assertEqual(result.decision, Decision.EXCLUDED)
        self.assertIn("OPERATING_LOSS", result.warnings)
        self.assertIn("MANAGEMENT_RISK_CONFIRMED", result.warnings)

    def test_repeated_dilution_is_warning_not_unconditional_exclusion(self) -> None:
        events = tuple(
            FinancingEvent(
                "005930", "CB", AS_OF - timedelta(days=365 * years), True, True,
                f"https://dart.fss.or.kr/{years}", Decimal("12.5"), "운영자금=100", True,
            )
            for years in (1, 2)
        )
        result = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=good_financial(), financing_events=events)
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("REPEATED_DILUTION", result.warnings)
        self.assertIn("REFIXING_PRESENT", result.warnings)
        self.assertEqual(result.metrics["max_dilution_ratio_pct_5y"], Decimal("12.5"))
        self.assertIn("조달 목적", " ".join(result.checks))

    def test_unverified_risk_histories_force_additional_check(self) -> None:
        result = screen_security(
            samsung(), make_bars(), as_of=AS_OF, financial=good_financial(),
            financing_data_available=False,
            management_data_available=False,
            catalyst_data_available=False,
        )
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("DILUTION_HISTORY_UNAVAILABLE", result.warnings)
        self.assertIn("MANAGEMENT_HISTORY_UNAVAILABLE", result.warnings)
        self.assertIn("CATALYST_HISTORY_UNAVAILABLE", result.warnings)

    def test_official_allegation_requires_review_but_does_not_claim_conviction(self) -> None:
        evidence = Evidence(
            "DART", "횡령ㆍ배임혐의발생", "https://dart.fss.or.kr/risk",
            datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC), True,
        )
        result = screen_security(
            samsung(), make_bars(), as_of=AS_OF, financial=good_financial(),
            management_risks=(ManagementRisk(
                "005930", False, "OFFICIAL_REVIEW", (evidence,),
            ),),
        )
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("MANAGEMENT_RISK_OFFICIAL_REVIEW", result.warnings)
        self.assertNotIn("MANAGEMENT_RISK_CONFIRMED", result.warnings)

    def test_etf_without_specialist_metrics_is_held_not_promoted(self) -> None:
        etf = Security("069500", "KODEX 200", Market.KOSPI, AssetType.ETF, CompanyKind.FUND, date(2002, 10, 14))
        result = screen_security(etf, make_bars("069500"), as_of=AS_OF)
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("ELIGIBLE_ETF", result.reasons)
        self.assertIn("ETF_METRICS_UNAVAILABLE", result.warnings)

    def test_etf_with_complete_specialist_metrics_can_be_candidate(self) -> None:
        etf = Security("069500", "KODEX 200", Market.KOSPI, AssetType.ETF, CompanyKind.FUND, date(2002, 10, 14))
        snapshot = EtfSnapshot(
            "069500", AS_OF.date(), AS_OF - timedelta(hours=1),
            Decimal("42050"), Decimal("7800000000000"), Decimal("0.12"),
            Decimal("0.15"), Decimal("0.15"), Decimal("35"),
            "https://data.krx.co.kr/example",
        )
        result = screen_security(etf, make_bars("069500"), as_of=AS_OF, etf_snapshot=snapshot)
        self.assertEqual(result.decision, Decision.CANDIDATE)
        self.assertIn("ETF_METRICS_COMPLETE", result.reasons)
        self.assertEqual(result.metrics["etf_net_assets"], Decimal("7800000000000"))

    def test_financial_company_uses_separate_contract_and_stays_under_specialist_review(self) -> None:
        bank = Security(
            "105560", "KB금융", Market.KOSPI, AssetType.COMMON,
            CompanyKind.FINANCIAL, date(2008, 10, 10),
        )
        general = FinancialSnapshot(
            "105560", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
            Decimal("-1"), Decimal("-1"), Decimal("-1"), (), (),
            "https://dart.fss.or.kr/general",
        )
        specialist = FinancialCompanySnapshot(
            "105560", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
            Decimal("14.2"), Decimal("9.1"), Decimal("0.7"), Decimal("0.5"),
            Decimal("180"), "배당 및 자사주 정책 확인", "https://fss.or.kr/example",
        )
        result = screen_security(
            bank, make_bars("105560"), as_of=AS_OF,
            financial=general, financial_company=specialist,
        )
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertNotIn("OPERATING_LOSS", result.warnings)
        self.assertIn("FINANCIAL_METRICS_COMPLETE", result.reasons)
        self.assertIn("FINANCIAL_SPECIALIST_REVIEW_REQUIRED", result.warnings)

    def test_unknown_company_kind_never_applies_general_company_ocf_exclusion(self) -> None:
        unknown = Security(
            "005930", "삼성전자", Market.KOSPI, AssetType.COMMON,
            CompanyKind.UNKNOWN, date(1975, 6, 11),
        )
        financial = FinancialSnapshot(
            "005930", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
            Decimal("100"), Decimal("-10"), None, (), (), "https://dart.fss.or.kr/example",
        )
        result = screen_security(unknown, make_bars(), as_of=AS_OF, financial=financial)
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("COMPANY_KIND_UNAVAILABLE", result.warnings)
        self.assertIn("OCF_NON_POSITIVE", result.warnings)
        self.assertIn("금융회사 여부", " ".join(result.checks))

    def test_missing_or_stale_ttm_profit_forces_buy_hold(self) -> None:
        missing = FinancialSnapshot(
            "005930", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
            Decimal("100"), Decimal("80"), Decimal("60"), (), (),
            "https://dart.fss.or.kr/missing",
        )
        missing_result = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=missing)
        self.assertEqual(missing_result.decision, Decision.BUY_HOLD)
        self.assertIn("PROFIT_PERIODS_UNAVAILABLE", missing_result.warnings)

        stale = FinancialSnapshot(
            "005930", date(2025, 12, 31), datetime(2026, 3, 20, tzinfo=UTC),
            Decimal("100"), Decimal("80"), Decimal("60"), (), (),
            "https://dart.fss.or.kr/stale",
            annual_operating_income=Decimal("100"),
            ttm_operating_income=Decimal("100"),
            ttm_period_end=date(2025, 12, 31),
            ttm_source_url="https://dart.fss.or.kr/stale",
        )
        stale_result = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=stale)
        self.assertEqual(stale_result.decision, Decision.BUY_HOLD)
        self.assertIn("TTM_STALE", stale_result.warnings)

    def test_future_financial_is_rejected(self) -> None:
        future = FinancialSnapshot(
            "005930", date(2026, 9, 30), AS_OF + timedelta(days=1),
            Decimal("1"), Decimal("1"), Decimal("1"), (), (), "https://dart.fss.or.kr/future",
        )
        with self.assertRaises(ContractError):
            screen_security(samsung(), make_bars(), as_of=AS_OF, financial=future)

    def test_top_candidates_are_deterministic_and_exclude_hard_failures(self) -> None:
        base = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=good_financial())
        variants = [base]
        for index in range(1, 7):
            variants.append(type(base)(
                f"{index:06d}", base.as_of,
                Decision.EXCLUDED if index == 6 else Decision.BUY_HOLD,
                Decimal(90 - index), base.reasons, base.warnings, base.checks, base.metrics,
            ))
        selected = select_top_candidates(variants)
        self.assertEqual(len(selected), 5)
        self.assertNotIn("000006", [item.symbol for item in selected])
        self.assertEqual(selected, select_top_candidates(list(reversed(variants))))


if __name__ == "__main__":
    unittest.main()
