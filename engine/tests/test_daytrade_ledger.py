from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from stock_assistant.daytrade import POLICY_VERSION
from stock_assistant.daytrade.ledger import DaytradeLedger, FixtureCosts
from stock_assistant.daytrade.market import QualityPolicy

ZERO_COSTS = FixtureCosts("SYNTHETIC-ZERO", "0", "0", "0", "0", "1", 0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.ledger = DaytradeLedger.create(self.root, synthetic=True, costs=ZERO_COSTS,
                                            quality=QualityPolicy(900, 30, 120))
        self.sequence = 0
        self.now = datetime.fromisoformat("2026-11-13T10:00:00+09:00")
        self.emit("SESSION", {"normal_session_verified": True})

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def emit(self, kind, data, *, at=None, event_id=None):
        self.sequence += 1
        self.now = datetime.fromisoformat(at) if at else self.now + timedelta(seconds=1)
        e = {"id": event_id or "e" + str(self.sequence), "sequence": self.sequence,
             "at": self.now.isoformat(), "policy_version": POLICY_VERSION, "kind": kind, "data": data}
        return self.ledger.apply(e)

    def book(self, code="005930", ask="100", bid="99", depth="100000", timestamp="AUTO"):
        # next event is received one second after self.now
        stamp = (self.now + timedelta(seconds=1)).isoformat() if timestamp == "AUTO" else timestamp
        eid = "book" + str(self.sequence + 1)
        self.emit("MARKET", {"feed": "orderbook", "symbol": code, "received_monotonic": self.sequence + 1,
            "payload": {"currency": "KRW", "timestamp": stamp,
                "asks": [{"price": ask, "volume": depth}], "bids": [{"price": bid, "volume": depth}]}}, event_id=eid)
        return eid

    def intent(self, code="005930", side="BUY", quantity=100):
        eid = "intent" + str(self.sequence + 1)
        self.emit("INTENT", {"side": side, "symbol": code, "quantity": quantity, "intent_id": eid})
        return eid

    def buy(self, code="005930", quantity=100, depth="100000"):
        i = self.intent(code, quantity=quantity)
        b = self.book(code, depth=depth)
        return self.emit("FILL", {"intent_id": i, "quote_event_id": b})

    def test_isolation_existing_file_and_legacy_ledger_preservation(self):
        legacy = self.root / ".runtime" / "hermes-paper.sqlite3"
        legacy.write_bytes(b"preserve")
        with self.assertRaises(ValueError): DaytradeLedger.create(self.root, path=legacy)
        with self.assertRaises(FileExistsError): DaytradeLedger.create(self.root)
        self.assertEqual(legacy.read_bytes(), b"preserve")
        self.assertEqual(self.ledger.meta["mode"], "OBSERVE_ONLY")

    def test_non_synthetic_ledger_cannot_create_execution_intents_or_ready_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = DaytradeLedger.create(folder)
            try:
                e = {"id": "try", "sequence": 1, "at": self.now.isoformat(), "policy_version": POLICY_VERSION,
                     "kind": "INTENT", "data": {"side": "BUY", "symbol": "005930", "quantity": 1}}
                with self.assertRaisesRegex(ValueError, "OBSERVE_ONLY"): ledger.apply(e)
                e.update(kind="PAPER_READY", data={})
                with self.assertRaises(ValueError): ledger.apply(e)
                self.assertEqual(ledger.state()["cash"], "10000000")
            finally: ledger.close()

    def test_partial_fill_quote_consumption_and_integer_quantity(self):
        r = self.buy(quantity=20, depth="10")
        self.assertEqual(r["status"], "PARTIAL")
        self.assertEqual(self.ledger.state()["positions"], {"005930": 10})
        fill = r["fill"]
        repeat = self.emit("FILL", {"intent_id": fill["intent_id"], "quote_event_id": fill["quote_event_id"]})
        self.assertEqual(repeat["status"], "UNFILLED_QUOTE_ALREADY_CONSUMED")
        self.assertTrue(self.ledger.audit()["cash_identity"])
        with self.assertRaises(ValueError): self.intent(quantity=True)

    def test_twenty_percent_boundary_and_transaction_rollback(self):
        self.buy(quantity=20000)
        self.assertEqual(self.ledger.state()["cash"], "8000000")
        before = self.ledger.state_hash
        i = self.intent("000660", quantity=20001)
        b = self.book("000660")
        before_fill = self.ledger.state_hash
        with self.assertRaisesRegex(ValueError, "twenty-percent"):
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})
        self.assertEqual(before_fill, self.ledger.state_hash)
        self.assertTrue(self.ledger.audit()["quantity_identity"])

    def test_fourth_distinct_position_is_rejected(self):
        for code in ["005930", "000660", "017960"]:
            self.buy(code)
        i = self.intent("240810")
        b = self.book("240810")
        with self.assertRaisesRegex(ValueError, "three-position"):
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})

    def test_aggregate_exposure_limit_uses_current_marked_equity(self):
        self.buy("005930", quantity=10000)
        self.buy("000660", quantity=10000)
        self.book("005930", ask="301", bid="300")
        self.book("000660", ask="301", bid="300")
        self.assertEqual(self.ledger.valuation(self.now)["equity"], Decimal("14000000"))
        i = self.intent("017960", quantity=25000)
        b = self.book("017960")
        with self.assertRaisesRegex(ValueError, "sixty-percent"):
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})

    def test_cash_identity_and_bid_valuation_are_independent_of_uncertain_marks(self):
        self.buy(quantity=1000)
        v = self.ledger.valuation(self.now)
        self.assertEqual(v["equity"], Decimal("9999000"))
        self.book(timestamp=None)
        self.assertEqual(self.ledger.valuation(self.now)["status"], "MARK_UNCERTAIN")
        self.assertIsNone(self.ledger.valuation(self.now)["equity"])
        self.assertTrue(self.ledger.audit()["cash_identity"])

    def test_unrealized_loss_latches_and_sells_continue_after_loss(self):
        self.buy(quantity=10000)
        self.book(ask="70", bid="69")
        state = self.ledger.state()
        self.assertIn("LOSS_LOCKED", state["risk"]["reasons"])
        self.assertEqual(next(iter(state["exit_requests"].values()))["reasons"], ["LOSS_EXIT"])
        with self.assertRaises(ValueError): self.intent("000660")
        i = self.intent(side="SELL", quantity=10000)
        b = self.book(ask="70", bid="69")
        self.emit("FILL", {"intent_id": i, "quote_event_id": b})
        self.assertFalse(self.ledger.state()["positions"])
        self.assertIn("LOSS_LOCKED", self.ledger.state()["risk"]["reasons"])

    def test_entry_cutoff_time_exit_requests_unflattened_and_next_day_lock(self):
        self.buy()
        self.emit("LOCK", {"lock": "RATE_LIMIT_DEGRADED", "reason": "MARKET_DATA_CHART"})
        self.emit("CLOCK", {}, at="2026-11-13T14:50:00+09:00")
        with self.assertRaises(ValueError): self.intent("000660")
        self.emit("CLOCK", {}, at="2026-11-13T15:15:00+09:00")
        self.assertTrue(self.ledger.state()["exit_requests"])
        cash = self.ledger.state()["cash"]
        self.emit("END_SESSION", {}, at="2026-11-13T15:30:00+09:00")
        self.assertEqual(self.ledger.state()["sessions"]["2026-11-13"], "UNFLATTENED")
        self.assertEqual(self.ledger.state()["cash"], cash)
        self.emit("SESSION", {"normal_session_verified": True}, at="2026-11-16T09:00:00+09:00")
        self.assertIn("PRIOR_DAY_UNFLATTENED", self.ledger.state()["risk"]["reasons"])
        self.assertIn("RATE_LIMIT_DEGRADED", self.ledger.state()["risk"]["reasons"])

    def test_future_or_pre_intent_book_never_fills_and_unknown_costs_never_default_zero(self):
        b = self.book()
        i = self.intent()
        with self.assertRaisesRegex(ValueError, "post-intent"):
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})
        with tempfile.TemporaryDirectory() as folder:
            ledger = DaytradeLedger.create(folder, synthetic=True)
            try:
                with self.assertRaisesRegex(ValueError, "explicit synthetic fixtures with costs"):
                    ledger.apply({"id": "f", "sequence": 1, "at": self.now.isoformat(), "policy_version": POLICY_VERSION, "kind": "FILL", "data": {}})
            finally: ledger.close()

    def test_favorable_old_quote_cannot_be_selected_after_a_newer_quote(self):
        i = self.intent()
        old = self.book(ask="100", bid="99")
        self.book(ask="200", bid="199")
        before = self.ledger.state_hash
        with self.assertRaisesRegex(ValueError, "latest observed"):
            self.emit("FILL", {"intent_id": i, "quote_event_id": old})
        self.assertEqual(before, self.ledger.state_hash)

    def test_closed_session_cannot_fake_a_liquidation_and_monotonic_regression_is_refused(self):
        self.buy()
        self.emit("CLOCK", {}, at="2026-11-13T15:30:00+09:00")
        self.assertEqual(self.ledger.state()["sessions"]["2026-11-13"], "UNFLATTENED")
        i = self.intent(side="SELL")
        b = self.book()
        self.assertEqual(self.emit("FILL", {"intent_id": i, "quote_event_id": b})["status"], "UNFILLED_SESSION_CLOSED")
        self.assertTrue(self.ledger.state()["positions"])
        with self.assertRaisesRegex(ValueError, "monotonic"):
            self.emit("MARKET", {"feed": "price", "symbol": "005930", "received_monotonic": 0,
                "payload": {"currency": "KRW", "timestamp": self.now.isoformat(), "lastPrice": "100"}})

    def test_duplicate_event_leaves_semantic_hash_and_database_bytes_unchanged(self):
        e = {"id": "repeat", "sequence": 2, "at": (self.now + timedelta(seconds=1)).isoformat(),
             "policy_version": POLICY_VERSION, "kind": "CLOCK", "data": {}}
        self.ledger.apply(e)
        before, raw = self.ledger.state_hash, self.ledger.path.read_bytes()
        self.assertTrue(self.ledger.apply(e)["duplicate"])
        self.assertEqual(before, self.ledger.state_hash)
        self.assertEqual(raw, self.ledger.path.read_bytes())
        with self.assertRaisesRegex(ValueError, "conflicting event"):
            self.ledger.apply(dict(e, data={"changed": True}))

    def test_reopen_preserves_locks_and_independent_fee_tax_cash_arithmetic(self):
        self.ledger.close()
        with tempfile.TemporaryDirectory() as folder:
            costs = FixtureCosts("SYNTHETIC-EXPLICIT", "100", "200", "300", "0", "1", 0)
            self.ledger = DaytradeLedger.create(folder, synthetic=True, costs=costs, quality=QualityPolicy(900, 30, 120))
            self.sequence = 0
            self.emit("SESSION", {"normal_session_verified": True})
            i = self.intent(quantity=10)
            b = self.book(ask="101", bid="100")
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})
            i = self.intent(side="SELL", quantity=10)
            b = self.book(ask="111", bid="110")
            self.emit("FILL", {"intent_id": i, "quote_event_id": b})
            # Independent literal cash-flow arithmetic: buy 1010 + 10; sell 1100 - 22 - 33.
            self.assertEqual(Decimal(self.ledger.state()["cash"]), Decimal("10000000") - 1010 - 10 + 1100 - 22 - 33)
            h = self.ledger.state_hash
            self.ledger.close()
            self.ledger = DaytradeLedger(folder)
            self.assertEqual(h, self.ledger.state_hash)
            self.ledger.close()
        # Return a connection rooted in setUp for tearDown; no existing file is changed.
        self.ledger = DaytradeLedger(self.root)

    def test_end_session_before_market_close_is_rejected_without_mutating_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = DaytradeLedger.create(folder)
            try:
                ledger.apply({"id": "preopen", "sequence": 0, "at": "2026-11-13T08:45:00+09:00",
                    "policy_version": POLICY_VERSION, "kind": "SESSION", "data": {"normal_session_verified": True}})
                before = ledger.state_hash
                for at in ("08:50:00", "15:29:59"):
                    with self.subTest(at=at), self.assertRaisesRegex(ValueError, "end-session"):
                        ledger.apply({"id": "too-early", "sequence": 1, "at": "2026-11-13T" + at + "+09:00",
                            "policy_version": POLICY_VERSION, "kind": "END_SESSION", "data": {}})
                    self.assertEqual(ledger.state_hash, before)
                    self.assertEqual(ledger.state()["sessions"]["2026-11-13"], "OBSERVING")
                outcome = ledger.apply({"id": "close", "sequence": 1, "at": "2026-11-13T15:30:00+09:00",
                    "policy_version": POLICY_VERSION, "kind": "END_SESSION", "data": {}})
                self.assertEqual(outcome["status"], "CLOSED")
            finally:
                ledger.close()

    def test_missing_close_event_marks_previous_session_unflattened_on_rollover(self):
        self.buy()
        self.emit("LOCK", {"lock": "RATE_LIMIT_DEGRADED", "reason": "MARKET_DATA_CHART"})
        previous = self.ledger.state()
        self.assertEqual(previous["sessions"]["2026-11-13"], "OBSERVING")
        self.emit("SESSION", {"normal_session_verified": True}, at="2026-11-16T09:00:00+09:00")
        state = self.ledger.state()
        self.assertEqual(state["sessions"]["2026-11-13"], "UNFLATTENED")
        self.assertEqual(state["sessions"]["2026-11-16"], "OBSERVING")
        self.assertIn("2026-11-13", state["risk"]["reasons"]["PRIOR_DAY_UNFLATTENED"])
        self.assertIn("RATE_LIMIT_DEGRADED", state["risk"]["reasons"])
        self.assertEqual(state["cash"], previous["cash"])
        self.assertEqual(state["positions"], previous["positions"])
        self.assertTrue(self.ledger.audit()["cash_identity"])
        with self.assertRaises(ValueError):
            self.intent("000660")
