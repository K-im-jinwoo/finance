from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_assistant.indicators import InsufficientHistory, price_features, trend_is_stable_or_rising, wilder_atr
from stock_assistant.models import OHLCV
from tests.helpers import make_bars


UTC = timezone.utc


class IndicatorTests(unittest.TestCase):
    def test_wilder_atr_uses_previous_close_and_requires_history(self) -> None:
        bars = []
        for index in range(15):
            close = Decimal(100 + index)
            bars.append(OHLCV(
                "005930", date(2026, 1, 1) + timedelta(days=index), close, close + 1,
                close - 1, close, 100, "TEST",
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            ))
        self.assertEqual(wilder_atr(bars), Decimal("2"))
        with self.assertRaises(InsufficientHistory):
            wilder_atr(bars[:-1])

    def test_price_features_detect_momentum_and_volume_ratio(self) -> None:
        features = price_features(make_bars())
        self.assertGreater(features.close, features.sma20)
        self.assertGreater(features.sma20, features.sma60)
        self.assertEqual(features.volume_ratio20, Decimal("3"))

    def test_turnover_tolerates_small_decline_only(self) -> None:
        self.assertTrue(trend_is_stable_or_rising((Decimal("10"), Decimal("9.6"))))
        self.assertFalse(trend_is_stable_or_rising((Decimal("10"), Decimal("9"))))
        self.assertIsNone(trend_is_stable_or_rising((Decimal("10"),)))


if __name__ == "__main__":
    unittest.main()

