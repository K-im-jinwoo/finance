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
    Evidence,
    FinancialSnapshot,
    FinancingEvent,
    ManagementRisk,
    Market,
    Security,
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
                f"https://dart.fss.or.kr/{years}",
            )
            for years in (1, 2)
        )
        result = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=good_financial(), financing_events=events)
        self.assertEqual(result.decision, Decision.BUY_HOLD)
        self.assertIn("REPEATED_DILUTION", result.warnings)
        self.assertIn("조달 목적", " ".join(result.checks))

    def test_etf_does_not_require_company_financials(self) -> None:
        etf = Security("069500", "KODEX 200", Market.KOSPI, AssetType.ETF, CompanyKind.FUND, date(2002, 10, 14))
        result = screen_security(etf, make_bars("069500"), as_of=AS_OF)
        self.assertEqual(result.decision, Decision.CANDIDATE)
        self.assertIn("ELIGIBLE_ETF", result.reasons)

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

