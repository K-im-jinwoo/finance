from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_assistant.validation import ContractError, assert_point_in_time, parse_krx_ohlcv_rows


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"


class ValidationTests(unittest.TestCase):
    def test_normalized_krx_fixture_parses_and_sorts(self) -> None:
        payload = json.loads((FIXTURES / "krx_ohlcv_normalized.json").read_text(encoding="utf-8"))
        rows = parse_krx_ohlcv_rows(payload["rows"], observed_at=datetime(2026, 9, 18, 9, tzinfo=UTC))
        self.assertEqual([row.trade_date.isoformat() for row in rows], ["2026-09-16", "2026-09-17"])

    def test_missing_and_duplicate_rows_fail_closed(self) -> None:
        observed = datetime(2026, 9, 18, tzinfo=UTC)
        with self.assertRaisesRegex(ContractError, "missing fields"):
            parse_krx_ohlcv_rows([{"symbol": "005930"}], observed_at=observed)
        row = {
            "symbol": "005930", "trade_date": "2026-09-17", "open": 10,
            "high": 11, "low": 9, "close": 10, "volume": 100,
        }
        with self.assertRaisesRegex(ContractError, "duplicate"):
            parse_krx_ohlcv_rows([row, row], observed_at=observed)

    def test_future_information_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractError, "later than as_of"):
            assert_point_in_time(
                datetime(2026, 9, 1, tzinfo=UTC),
                published_at=datetime(2026, 9, 2, tzinfo=UTC),
                label="filing",
            )


if __name__ == "__main__":
    unittest.main()

