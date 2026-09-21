from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.models import Decision, Holding, ScreeningResult
from stock_assistant.repository import StockRepository


UTC = timezone.utc


class RepositoryTests(unittest.TestCase):
    def test_holding_upsert_is_idempotent_and_preserves_broker_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.replace_holding(Holding("미래에셋", "007660", Decimal("10"), Decimal("130000")))
            repository.replace_holding(Holding("미래에셋", "007660", Decimal("20"), Decimal("102000")))
            repository.replace_holding(Holding("토스증권", "007660", Decimal("1"), Decimal("100000")))
            holdings = repository.list_holdings()
            self.assertEqual(len(holdings), 2)
            self.assertEqual(holdings[0].quantity, Decimal("20"))

    def test_screening_result_upsert_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            result = ScreeningResult(
                "005930", datetime(2026, 9, 21, tzinfo=UTC), Decision.BUY_HOLD,
                Decimal("50"), (), (), ("확인",), {},
            )
            repository.save_screening_results("R-1", [result])
            repository.save_screening_results("R-1", [result])
            with repository._connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM screening_results").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

