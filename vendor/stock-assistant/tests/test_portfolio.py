from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from stock_assistant.models import Decision, Holding, ScreeningResult, ThesisCard, ThesisStatus
from stock_assistant.portfolio import aggregate_holdings, review_position


UTC = timezone.utc


class PortfolioTests(unittest.TestCase):
    def test_holdings_are_aggregated_across_brokers_with_weighted_average(self) -> None:
        positions = aggregate_holdings([
            Holding("미래에셋", "007660", Decimal("10"), Decimal("130000")),
            Holding("토스증권", "007660", Decimal("20"), Decimal("88000")),
        ])
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].quantity, Decimal("30"))
        self.assertEqual(positions[0].average_price, Decimal("102000"))
        self.assertEqual(positions[0].brokers, ("미래에셋", "토스증권"))

    def test_profit_scenario_includes_partial_profit_and_remainder_rules(self) -> None:
        position = aggregate_holdings([Holding("토스증권", "007660", Decimal("1"), Decimal("100000"))])[0]
        thesis = ThesisCard(
            "T-1", "007660", datetime(2026, 9, 1, tzinfo=UTC),
            ("공식 계약 공시 확인",), ("반도체 업황 동행 기대",),
            ("계약 취소 또는 영업현금흐름 음수",), ("다음 분기 실적 확인",), ThesisStatus.APPROVED,
        )
        screening = ScreeningResult(
            "007660", datetime(2026, 9, 20, tzinfo=UTC), Decision.CANDIDATE,
            Decimal("80"), ("CATALYST_CONFIRMED",), (), (), {},
        )
        review = review_position(position, current_price=Decimal("111000"), thesis=thesis, screening=screening)
        self.assertIn("PARTIAL_PROFIT_REVIEW", review.actions)
        self.assertIn("HOLD_REMAINDER_WITH_INVALIDATION", review.actions)

    def test_loss_does_not_become_automatic_averaging_down_signal(self) -> None:
        position = aggregate_holdings([Holding("미래에셋", "007660", Decimal("1"), Decimal("130000"))])[0]
        thesis = ThesisCard(
            "T-2", "007660", datetime(2026, 9, 1, tzinfo=UTC),
            ("사용자 매수 기록",), ("업종 동행 기대",), ("저점 이탈",), ("공시 확인",), ThesisStatus.APPROVED,
        )
        screening = ScreeningResult(
            "007660", datetime(2026, 9, 20, tzinfo=UTC), Decision.BUY_HOLD,
            Decimal("40"), (), ("DATA_UNAVAILABLE",), ("공시 확인",), {},
        )
        review = review_position(position, current_price=Decimal("65000"), thesis=thesis, screening=screening)
        self.assertIn("DO_NOT_AVERAGE_DOWN_WITHOUT_REVALIDATION", review.actions)
        self.assertIn("NEW_BUY_HOLD", review.actions)


if __name__ == "__main__":
    unittest.main()

