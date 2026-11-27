from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path) for path in ("src", "engine/src")]

from research_dashboard.longterm_ledger import LongTermAccounts
from research_dashboard.longterm_rules import LongTermPolicy
from stock_assistant.models import OHLCV
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData


def at(day, hour=9):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def row(number, decision="BUY_RECOMMENDATION", **fields):
    return {"symbol": f"{number:06}", "name": f"Issuer {number}", "rank": number,
            "action_decision": decision, **fields}


class LongTermLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", {"STOCK_DATABASE_PATH": "", "STOCK_UNIFIED_RESEARCH_DATABASE": "",
                                  "LONGTERM_LEDGER_PATH": ""}).start()
        self.stock = StockRepository(self.root / ".runtime/stock-assistant.sqlite3")
        self.source = ResearchData(self.root / ".runtime/unified-research-inputs.sqlite3",
                                   source_database=self.stock.database_path)
        self.ledger = LongTermAccounts(self.root)

    def report(self, day, candidates=(), evaluations=None, *, hour=9, **fields):
        identifier = f"UR-{day * 24 + hour:024x}"
        self.source.put("unified_report", "ALL", {
            "report_id": identifier, "contract": "UNIFIED_RESEARCH_V1", "as_of": at(day, hour).isoformat(),
            "expected_price_session": at(day).date().isoformat(), "candidates": list(candidates),
            "evaluations": list(candidates if evaluations is None else evaluations), **fields,
        }, observed_at=at(day, hour))
        return identifier

    def bar(self, day, number, price="100000", volume=1, source="KRX_OPEN_API", observed=None):
        value = Decimal(price)
        self.stock.save_bars([OHLCV(f"{number:06}", date(2026, 11, day), value, value, value, value,
                                   volume, source, observed or at(day, 8))])

    def test_forward_start_rank_limit_floor_fees_and_idempotency(self):
        self.report(9, [row(99)])
        identifier = self.report(10, [row(i) for i in (6, 3, 2, 1, 5, 4)])
        self.bar(10, 1)
        self.ledger.initialize(now=at(10))
        self.assertEqual(self.ledger.overview()["pending_count"], 5)
        self.assertEqual(self.ledger.account()["fills"], [])
        for i in range(1, 7):
            self.bar(11, i, "100001")
        result = self.ledger.refresh(now=at(11))
        self.assertEqual([item["report_id"] for item in result["reports"]], [identifier])
        self.assertEqual([item["symbol"] for item in result["fills"]], [f"{i:06}" for i in range(1, 6)])
        self.assertEqual(result["fills"][0]["quantity"], 19)
        self.assertEqual(result["fills"][0]["fee"], "2850")
        self.assertEqual(result["equity"][0]["date"], "2026-11-11")
        self.assertEqual(result, self.ledger.refresh(now=at(11)))
        self.assertEqual(LongTermPolicy().fee(Decimal("1000")), Decimal(2))

    def test_official_close_only_and_missing_mark(self):
        self.report(10, [row(1), row(2)], expected_price_session="2026-11-11")
        self.ledger.initialize(now=at(10))
        self.bar(11, 1)
        self.bar(12, 1, volume=0)
        self.bar(12, 2, source="UNOFFICIAL")
        self.bar(12, 3)
        self.assertEqual(self.ledger.refresh(now=at(12))["fills"], [])
        self.bar(13, 1)
        self.bar(13, 2, observed=at(17, 8))
        result = self.ledger.refresh(now=at(13))
        self.assertEqual([item["symbol"] for item in result["fills"]], ["000001"])
        self.assertEqual(result["pending"][0]["reason"], "PENDING_UNTRADABLE_CLOSE")
        self.bar(16, 2)
        result = self.ledger.refresh(now=at(16))
        self.assertIsNone(result["equity"][-1]["total_assets"])
        self.assertEqual(result["pending"][0]["reason"], "MISSING_POSITION_CLOSE")
        self.assertEqual(result["missing_close_count"], 1)
        self.bar(16, 1)
        result = self.ledger.refresh(now=at(16))
        self.assertEqual(len(result["fills"]), 2)

    def test_risk_cancels_pending_and_exit_funds_buy_with_slot(self):
        self.report(10, [row(i) for i in range(1, 6)])
        self.ledger.initialize(now=at(10))
        for i in range(1, 5):
            self.bar(11, i, "1980000")
        self.ledger.refresh(now=at(11))
        self.report(11, [row(6)], [row(1, "WATCH", risk_penalty="1"), row(2, "EXCLUDED"),
                                    row(3, "VERIFY_PENDING"), row(4, "WATCH"), row(5, "EXCLUDED")])
        result = self.ledger.refresh(now=at(11))
        self.assertEqual([item["side"] for item in result["pending"]], ["SELL", "SELL", "BUY"])
        statuses = {item["symbol"]: item["review_status"] for item in result["positions"]}
        self.assertEqual(statuses["000003"], "REVIEW_REQUIRED")
        self.assertEqual(statuses["000004"], "HOLD_NO_EXIT_TRIGGER")
        self.assertIn("CANCELLED_BY_RISK_OR_EXCLUSION", [item["reason"] for item in result["orders"]])
        for i in range(1, 7):
            self.bar(12, i, "1980000" if i != 5 else "100000")
        result = self.ledger.refresh(now=at(12))
        self.assertEqual([(item["side"], item["symbol"]) for item in result["fills"]][-3:],
                         [("SELL", "000001"), ("SELL", "000002"), ("BUY", "000006")])

    def test_slot_and_cash_defer(self):
        self.report(10, [row(i) for i in range(1, 6)])
        self.ledger.initialize(now=at(10))
        for i in range(1, 7):
            self.bar(11, i)
        self.ledger.refresh(now=at(11))
        self.report(11, [row(6)])
        for i in range(1, 7):
            self.bar(12, i)
        self.assertEqual(self.ledger.refresh(now=at(12))["pending"][0]["reason"], "POSITION_LIMIT")

        self.report(12, [row(7)], [row(1, "EXCLUDED")])
        for i in range(1, 8):
            self.bar(13, i, "1" if i == 1 else "100000000")
        result = self.ledger.refresh(now=at(13))
        self.assertEqual([item["reason"] for item in result["pending"]], ["INSUFFICIENT_CASH", "INSUFFICIENT_CASH"])

    def test_pause_resume_and_missing_evaluation(self):
        self.report(10, [row(1), row(2)])
        self.ledger.initialize(now=at(10))
        self.bar(11, 1)
        self.ledger.refresh(now=at(11))
        self.ledger.control("pause", now=at(11))
        self.report(12, [row(3)], [])
        self.bar(12, 1)
        self.bar(12, 2)
        result = self.ledger.refresh(now=at(12))
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["positions"][0]["review_status"], "REVIEW_REQUIRED")
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual([item["symbol"] for item in result["pending"]], ["000002"])
        self.ledger.control("resume", now=at(12))
        self.bar(13, 1)
        self.bar(13, 2)
        self.assertEqual(len(self.ledger.refresh(now=at(13))["fills"]), 2)

    def test_stale_initial_report_does_not_replay_old_bars(self):
        self.report(10, [row(1)])
        self.bar(11, 1)
        self.bar(12, 1)
        self.ledger.initialize(now=at(12))
        self.assertEqual(self.ledger.refresh(now=at(12))["fills"], [])
        self.bar(13, 1)
        self.assertEqual(self.ledger.refresh(now=at(13))["fills"][0]["trade_date"], "2026-11-13")

    def test_missed_batch_retains_chronology_and_uses_exchange_close(self):
        self.report(10, [row(1), row(2)])
        self.ledger.initialize(now=at(10))
        self.bar(11, 1)
        self.bar(12, 2, observed=at(12, 7))
        self.bar(12, 1)
        self.bar(13, 1)
        self.bar(13, 2)
        calendar = {"source": "TOSS_SECURITIES_OPEN_API", "calendar": {
            "today": {"date": "2026-11-12", "integrated": {"regularMarket": {
                "endTime": "2026-11-12T16:30:00+09:00",
            }}},
        }}
        self.source.put("market_calendar", "KR", calendar, observed_at=at(12, 0))
        self.report(12, [], [row(1, "WATCH"), row(2, "VERIFY_PENDING")])
        result = self.ledger.refresh(now=at(13))
        self.assertEqual([(item["symbol"], item["trade_date"]) for item in result["fills"]],
                         [("000001", "2026-11-11"), ("000002", "2026-11-13")])
        self.assertEqual([item["review_status"] for item in result["positions"]],
                         ["HOLD_NO_EXIT_TRIGGER", "REVIEW_REQUIRED"])

    def test_risk_evidence_cancels_before_close_while_paused_exits_still_settle(self):
        self.report(10, [row(1), row(2)])
        self.ledger.initialize(now=at(10))
        self.bar(11, 1)
        self.ledger.refresh(now=at(11))
        self.ledger.control("pause", now=at(11))
        self.report(12, [], [row(1, "WATCH", warnings=["⚠️ 확인 필요: 사업 훼손(계약해지/거래정지/회생) 관련 기사/공시 1건"]),
                             row(2, "VERIFY_PENDING", risk_signals=[{"category": "MANAGEMENT"}])], hour=5)
        self.bar(12, 1)
        self.bar(12, 2)
        self.bar(13, 1)
        self.bar(13, 2)
        result = self.ledger.refresh(now=at(13))
        self.assertEqual([(fill["side"], fill["symbol"]) for fill in result["fills"]],
                         [("BUY", "000001"), ("SELL", "000001")])
        self.assertEqual(result["fills"][-1]["trade_date"], "2026-11-13")
        self.assertEqual(result["orders"][1]["status"], "CANCELLED")

    def test_concurrent_batches_commit_a_single_fill(self):
        self.report(10, [row(1)])
        self.ledger.initialize(now=at(10))
        self.bar(11, 1)
        stock_before = self.stock.database_path.read_bytes()
        inputs_before = self.source.path.read_bytes()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.ledger.refresh(now=at(11)), range(2)))
        self.assertEqual(len(results[0]["fills"]), 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.stock.database_path.read_bytes(), stock_before)
        self.assertEqual(self.source.path.read_bytes(), inputs_before)

    def test_missing_ledger_read_and_output_path_collision_are_safe(self):
        self.assertEqual(self.ledger.overview()["status"], "NO_ACCOUNT")
        self.assertFalse(self.ledger.path.exists())
        original = self.stock.database_path.read_bytes()
        with patch.dict("os.environ", {"LONGTERM_LEDGER_PATH": str(self.stock.database_path)}):
            with self.assertRaises(ValueError):
                LongTermAccounts(self.root).initialize(now=at(10))
        self.assertEqual(original, self.stock.database_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
