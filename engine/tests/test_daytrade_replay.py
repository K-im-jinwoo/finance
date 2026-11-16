import copy
from decimal import Decimal
import json
from pathlib import Path
import random
import tempfile
import unittest

from stock_assistant.daytrade.replay import ReplayEngine, replay_dataset
from stock_assistant.daytrade.ledger import DaytradeLedger, FixtureCosts
from stock_assistant.daytrade.market import QualityPolicy

FIXTURE = Path(__file__).parent / "fixtures/daytrade_synthetic.json"


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_two_independent_runs_hash_equal_and_cash_matches_literal_calculation(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = replay_dataset(self.dataset, a)
            second = replay_dataset(self.dataset, b)
            self.assertEqual(first, second)
            self.assertEqual(first["fills"], 2)
            self.assertEqual(Decimal(first["cash_audit"]["expected_cash"]), Decimal(10000000) - 1010 - 10 + 1100 - 22 - 33)
            self.assertFalse(first["paper_ready"])

    def test_explicit_sequence_makes_shuffled_input_deterministic(self):
        shuffled = copy.deepcopy(self.dataset)
        random.Random(5).shuffle(shuffled["events"])
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first, second = replay_dataset(self.dataset, a), replay_dataset(shuffled, b)
            self.assertEqual(first["ledger_hash"], second["ledger_hash"])
            self.assertNotEqual(first["dataset_hash"], second["dataset_hash"])

    def test_replay_duplicate_events_after_reopen_do_not_touch_database(self):
        with tempfile.TemporaryDirectory() as root:
            replay_dataset(self.dataset, root)
            ledger = DaytradeLedger(root)
            try:
                before, raw = ledger.state_hash, ledger.path.read_bytes()
                ReplayEngine(ledger, seed=7).run(self.dataset["events"] * 2)
                self.assertEqual(before, ledger.state_hash)
                self.assertEqual(raw, ledger.path.read_bytes())
            finally: ledger.close()

    def test_ambiguous_sequence_future_information_and_policy_change_are_rejected(self):
        for change in ("sequence", "future", "policy"):
            d = copy.deepcopy(self.dataset)
            if change == "sequence": d["events"][2]["sequence"] = 1
            if change == "future": d["events"][2]["data"]["payload"]["timestamp"] = "2099-01-01T00:00:00Z"
            if change == "policy": d["events"][1]["policy_version"] = "different"
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
                replay_dataset(d, root)

    def test_seeded_runtime_429_replay_preserves_degraded_lock_and_resumes_identically(self):
        d = copy.deepcopy(self.dataset)
        d["events"] = [d["events"][0]]
        for i, mono in enumerate([1, 10, 20], 1):
            d["events"].append({"id": "http" + str(i), "sequence": i, "at": f"2026-11-13T10:00:{i:02}+09:00",
                "policy_version": d["policy_version"], "kind": "HTTP_RESPONSE", "data": {"path": "/api/v1/candles", "status": 429,
                    "payload": {"error": {"code": "rate-limit-exceeded", "message": "ignored"}}, "headers": {"X-RateLimit-Remaining": "19"}, "monotonic": mono}})
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = replay_dataset(d, a)
            ledger = DaytradeLedger.create(b, synthetic=True, replay_seed=7,
                quality=QualityPolicy(**d["quality"]), costs=FixtureCosts(**d["fixture_costs"]))
            ReplayEngine(ledger, seed=7).run(d["events"][:2])
            ledger.close()
            ledger = DaytradeLedger(b)
            try:
                second = ReplayEngine(ledger, seed=7).run(d["events"])
                self.assertEqual(first["ledger_hash"], second["ledger_hash"])
                self.assertIn("RATE_LIMIT_DEGRADED", ledger.state()["risk"]["reasons"])
            finally: ledger.close()

    def test_observation_dataset_never_accepts_fixture_execution(self):
        d = copy.deepcopy(self.dataset)
        d["dataset_kind"] = "OBSERVATION"
        d["fixture_costs"] = None
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(ValueError, "OBSERVE_ONLY"):
            replay_dataset(d, root)

    def test_participation_latency_and_extra_slippage_are_explicit_fixture_assumptions(self):
        d = copy.deepcopy(self.dataset)
        d["fixture_costs"]["participation"] = "0.05"
        with tempfile.TemporaryDirectory() as root:
            r = replay_dataset(d, root)
            self.assertEqual(Decimal(r["cash_audit"]["expected_cash"]), Decimal(10000000) - 505 - 5 + 550 - 11 - 17)
        d = copy.deepcopy(self.dataset)
        d["fixture_costs"]["latency_ms"] = 1500
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(ValueError, "post-latency"):
            replay_dataset(d, root)
        d["fixture_costs"]["latency_ms"] = 1000
        d["fixture_costs"]["extra_slippage_bps"] = "100"
        with tempfile.TemporaryDirectory() as root:
            r = replay_dataset(d, root)
            self.assertEqual(Decimal(r["cash_audit"]["expected_cash"]), Decimal(10000000) - Decimal("1020.10") - 10 + 1089 - 22 - 33)

    def test_cli_runs_without_network_or_websockets_and_preserves_existing_ledger(self):
        import contextlib
        import io
        from unittest.mock import patch
        from stock_assistant.daytrade.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            out = io.StringIO()
            with patch.dict("sys.modules", {"websockets": None}), patch("socket.create_connection", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(out):
                code = main(["replay", str(FIXTURE), "--root", root])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["fills"], 2)
            path = Path(root) / ".runtime/intraday-paper.sqlite3"
            raw = path.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["replay", str(FIXTURE), "--root", root]), 1)
            self.assertEqual(raw, path.read_bytes())
            ledger = DaytradeLedger(root)
            try:
                with self.assertRaisesRegex(ValueError, "seed"):
                    ReplayEngine(ledger, seed=8)
            finally: ledger.close()
