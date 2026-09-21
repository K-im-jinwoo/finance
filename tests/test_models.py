from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from stock_assistant.models import (
    AssetType,
    CompanyKind,
    Evidence,
    Holding,
    Market,
    OHLCV,
    Security,
)


UTC = timezone.utc


class ModelTests(unittest.TestCase):
    def test_security_rejects_wrong_symbol_and_etf_kind_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "six-digit"):
            Security("ABC", "bad", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(2020, 1, 1))
        with self.assertRaisesRegex(ValueError, "ETF"):
            Security("123456", "ETF", Market.KOSPI, AssetType.ETF, CompanyKind.GENERAL, date(2020, 1, 1))

    def test_ohlcv_rejects_invalid_price_geometry(self) -> None:
        with self.assertRaisesRegex(ValueError, "high"):
            OHLCV(
                "005930", date(2026, 1, 1), Decimal("10"), Decimal("9"),
                Decimal("8"), Decimal("10"), 1, "TEST",
                datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_evidence_requires_timezone_and_publication_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            Evidence("NEWS", "title", "https://example.test", datetime(2026, 1, 1), datetime(2026, 1, 1), False)
        with self.assertRaisesRegex(ValueError, "before publication"):
            Evidence(
                "DART", "title", "https://example.test",
                datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC), True,
            )

    def test_holding_requires_positive_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            Holding("토스증권", "005930", Decimal("0"), Decimal("70000"))


if __name__ == "__main__":
    unittest.main()

