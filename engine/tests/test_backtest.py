from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from stock_assistant.backtest import event_study
from tests.helpers import make_bars


class BacktestTests(unittest.TestCase):
    def test_entry_uses_next_trading_day_open_and_cost_is_applied(self) -> None:
        bars = make_bars(count=70)
        signal = bars[10].trade_date
        result = event_study(bars, signal_date=signal, horizons=(5,), round_trip_cost_bps=Decimal("30"))
        self.assertEqual(result.entry_date, bars[11].trade_date)
        self.assertEqual(result.entry_price, bars[11].open)
        expected_gross = bars[16].close / bars[11].open - Decimal("1")
        self.assertEqual(result.horizons[0].gross_return, expected_gross)
        self.assertEqual(result.horizons[0].net_return, expected_gross - Decimal("0.003"))

    def test_missing_future_horizon_returns_none(self) -> None:
        bars = make_bars(count=10)
        result = event_study(bars, signal_date=bars[-2].trade_date, horizons=(5,))
        self.assertIsNone(result.horizons[0].net_return)

    def test_duplicate_date_is_rejected(self) -> None:
        bars = make_bars(count=10)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            event_study(bars + [bars[-1]], signal_date=date(2026, 1, 2))


if __name__ == "__main__":
    unittest.main()

