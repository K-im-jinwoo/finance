import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "engine/src"), str(ROOT / "scripts")]
from make_validation_dataset import make
from research_dashboard.contracts import validate_dataset, digest
from research_dashboard.engine import Engine
from research_dashboard.rules import Policy, invalidation
from research_dashboard.store import Store
from research_dashboard.notifications import drafts

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.data = make()
        self.engine = Engine(self.data, Policy())

    def until(self, kind, occurrence=1):
        count = 0
        for event in self.data["events"]:
            self.engine.process(event)
            if event["type"] == kind:
                count += 1
                if count == occurrence:
                    break

    def test_morning_same_day_open_and_no_repeat_buy(self):
        self.until("OPEN")
        trades = self.engine.s["trades"]
        self.assertEqual(len(trades), 5)
        self.assertTrue(all(t["filled_at"].startswith("2026-04-13T09:00") for t in trades))
        for event in self.data["events"]:
            if event["type"] == "REPORT" and "20:00" in event["at"]:
                self.engine.process(event)
                break
        self.assertEqual(len(self.engine.s["positions"]), 5)
        self.assertFalse(self.engine.s["pending"])

    def test_evening_signal_uses_future_session(self):
        data = deepcopy(self.data)
        data["events"][0]["at"] = data["events"][0]["observed_at"] = data["events"][0]["input_available_at"] = "2026-04-13T20:00:00+09:00"
        data["events"] = [data["events"][0], next(e for e in data["events"] if e["type"] == "OPEN" and e["at"].startswith("2026-04-14"))]
        result = Engine(data, Policy()).run()
        self.assertTrue(all(t["filled_at"].startswith("2026-04-14") for t in result["state"]["trades"]))

    def test_exact_open_signal_cannot_fill_same_open(self):
        event = deepcopy(self.data["events"][0])
        event["at"] = event["observed_at"] = event["input_available_at"] = self.data["events"][1]["at"]
        self.engine.process(event)
        self.engine.process(self.data["events"][1])
        self.assertFalse(self.engine.s["trades"])

    def test_duplicate_event_and_content_conflict(self):
        self.until("OPEN")
        before = deepcopy(self.engine.s)
        self.engine.process(self.data["events"][1])
        self.assertEqual(before, self.engine.s)
        changed = deepcopy(self.data["events"][1])
        changed["prices"]["900001"] = "1"
        with self.assertRaises(ValueError):
            self.engine.process(changed)

    def test_price_boundary_equal_and_financial_zero(self):
        event = deepcopy(next(e for e in self.data["events"] if e["type"] == "CHECK"))
        event["price"]["closes"] = ["100"] * 20
        result = invalidation({"kind": "GENERAL"}, event)
        self.assertEqual(result[0]["status"], "VALID")
        for field in ("operating_income", "annual_operating_income", "ttm_operating_income", "ocf"):
            changed = deepcopy(event)
            changed["financial"][field] = "0"
            self.assertEqual(invalidation({"kind": "GENERAL"}, changed)[1]["status"], "INVALID")

    def test_missing_stale_specialist_and_disclosure(self):
        event = deepcopy(next(e for e in self.data["events"] if e["type"] == "CHECK"))
        event.pop("financial")
        event["price"]["fresh"] = False
        results = invalidation({"kind": "GENERAL"}, event)
        self.assertEqual([r["status"] for r in results], ["UNKNOWN","UNKNOWN","REVIEW_REQUIRED"])
        for kind in ("FINANCIAL", "ETF"):
            self.assertEqual(invalidation({"kind":kind}, event)[1]["status"], "NOT_APPLICABLE")

    def test_rank_drop_keeps_holding_and_later_invalidation_sells(self):
        result = self.engine.run()
        trades = result["state"]["trades"]
        sells = [t for t in trades if t["side"] == "SELL"]
        self.assertEqual(len(sells), 1)
        self.assertTrue(sells[0]["filled_at"].startswith("2026-04-15"))
        self.assertTrue(sells[0]["signal_at"].startswith("2026-04-14T20:01"))
        self.assertEqual(result["metrics"]["completed_round_trips"], 1)

    def test_reentry_requires_new_resolved_data_and_new_report(self):
        result = self.engine.run()
        buys = [t for t in result["state"]["trades"] if t["symbol"] == "900001" and t["side"] == "BUY"]
        self.assertEqual(len(buys), 2)
        self.assertTrue(buys[-1]["signal_at"].startswith("2026-04-16T08:30"))
        self.assertTrue(any(o["state"] == "DEFER_UNRESOLVED_INVALIDATION" for o in result["state"]["observations"]))

    def test_cash_fees_budget_and_accounting_identity(self):
        self.until("OPEN")
        for t in self.engine.s["trades"]:
            self.assertLessEqual(int(t["quantity"])*int(t["price"])+int(t["fee"]), 2000000)
        self.assertGreaterEqual(int(self.engine.s["cash"]),0)
        result = self.engine.run()
        equity = result["state"]["equity"][-1]
        self.assertEqual(Decimal(equity["total_assets"])-Decimal("10000000"), Decimal(result["metrics"]["realized_pnl"])+Decimal(equity["unrealized_pnl"]))

    def test_cash_insufficient_defers_instead_of_reducing(self):
        self.until("REPORT")
        self.engine.s["cash"] = "1"
        self.engine.s["positions"]["900006"] = {"quantity":100}
        # Test allocation path without corrupting the ledger assertion.
        self.engine.open(self.data["events"][1])
        self.assertFalse(self.engine.s["trades"])
        self.assertTrue(all(o.get("reason") in (None,"LOT_TOO_EXPENSIVE","CASH_INSUFFICIENT") for o in self.engine.s["observations"]))

    def test_suspension_missing_open_and_missing_nav(self):
        self.until("REPORT")
        event = deepcopy(self.data["events"][1])
        event["suspended"] = ["900001"]
        event["prices"].pop("900002")
        self.engine.process(event)
        self.assertEqual(len(self.engine.s["trades"]), 3)
        self.assertEqual(len(self.engine.s["pending"]), 2)

    def test_pause_cancels_pending_and_requires_new_signal(self):
        self.until("REPORT")
        self.engine.process({"id":"pause", "type":"PAUSE", "at":"2026-04-13T08:31:00+09:00"})
        self.assertFalse(self.engine.s["pending"])
        self.engine.process(self.data["events"][1])
        self.assertFalse(self.engine.s["trades"])
        self.engine.process({"id":"resume", "type":"RESUME", "at":"2026-04-13T09:01:00+09:00"})
        self.assertFalse(self.engine.s["pending"])
        for event in self.data["events"][2:]:
            self.engine.process(event)
        self.assertTrue(self.engine.s["trades"][0]["filled_at"].startswith("2026-04-14"))

    def test_missing_mark_never_becomes_zero(self):
        self.until("OPEN")
        self.engine.process({"id":"missing-mark", "type":"MARK", "at":"2026-04-13T16:00:00+09:00", "prices":{},"source":"SYNTHETIC","observed_at":"2026-04-13T16:00:00+09:00"})
        self.assertIsNone(self.engine.s["equity"][-1]["total_assets"])
        self.assertEqual(len(self.engine.s["equity"][-1]["missing_symbols"]), 5)
        self.assertIsNone(self.engine.result()["metrics"]["max_drawdown"])

    def test_future_observation_or_filing_rejected(self):
        for field in ("observed_at", "published_at", "available_at"):
            data = deepcopy(self.data)
            check = next(e for e in data["events"] if e["type"] == "CHECK")
            check["financial"][field] = "2027-01-01T00:00:00+09:00"
            with self.assertRaises(ValueError):
                validate_dataset(data)

    def test_end_preserves_open_positions_and_no_forced_sale(self):
        result = self.engine.run()
        self.assertEqual(result["state"]["status"], "COMPLETED")
        self.assertGreater(result["metrics"]["open_positions"],0)
        self.assertEqual(len([t for t in result["state"]["trades"] if t["side"]=="SELL"]),1)

    def test_transaction_restart_and_new_cost_experiment_preserves_results(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"paper.sqlite3"
            store = Store(path)
            eid = store.create(self.data, Policy())
            store.apply(eid,"start")
            store.apply(eid,limit=3)
            store = Store(path)
            result = store.apply(eid,limit=100)
            self.assertEqual(result["state"], Engine(self.data,Policy()).run()["state"])
            original = digest(result)
            other = store.create(self.data,Policy(round_trip_bps="60"))
            self.assertNotEqual(other,eid)
            self.assertEqual(digest(store.get(eid)), original)
            self.assertEqual(digest(store.apply(eid,limit=100)), original)

    def test_unknown_financial_missing_value_is_not_profit(self):
        event=deepcopy(next(e for e in self.data["events"] if e["type"]=="CHECK"))
        event["financial"]["ocf"]=None
        self.assertEqual(invalidation({"kind":"GENERAL"},event)[1]["status"],"UNKNOWN")

    def test_notice_deep_links_and_no_delivery(self):
        result=self.engine.run()
        result["experiment_id"]="E-test"
        items=drafts(result)
        self.assertTrue(all(i["detail_url"] is None and i["delivery"]=="DISABLED_PENDING_APPROVAL" for i in items))
        trade=next(i for i in items if i["kind"]=="SELL")
        self.assertIn("experiment=E-test",trade["detail_path"])
        self.assertIn("trade=",trade["detail_path"])
        self.assertIn("report=",trade["detail_path"])
        with self.assertRaises(ValueError):
            drafts(result,"http://insecure.example")

    def test_experimental_buy_hold_keeps_original_decision(self):
        self.until("OPEN")
        self.assertEqual(self.engine.s["positions"]["900001"]["original_decision"],"BUY_HOLD")
        self.assertEqual(self.engine.s["reports"][0]["candidates"][0]["decision"],"BUY_HOLD")

    def test_sixth_candidate_is_deferred_at_limit(self):
        self.until("OPEN")
        event=deepcopy(self.data["events"][2])
        candidate=deepcopy(event["candidates"][1])
        candidate["symbol"]="900006"
        event["candidates"]=[candidate]
        self.engine.process(event)
        self.engine.process(next(e for e in self.data["events"] if e["type"]=="OPEN" and e["at"].startswith("2026-04-14")))
        self.assertNotIn("900006",self.engine.s["positions"])
        self.assertTrue(any(o.get("reason")=="POSITION_LIMIT" for o in self.engine.s["observations"]))

    def test_repeated_invalidation_and_summary_do_not_repeat_notices(self):
        self.until("CHECK",6)
        event=deepcopy(next(e for e in self.data["events"] if e["type"]=="CHECK" and e["at"].startswith("2026-04-14")))
        event["id"]="repeat-invalid"
        before=sum(e["kind"]=="INVALIDATION" for e in self.engine.s["outbox"])
        self.engine.process(event)
        self.assertEqual(sum(e["kind"]=="INVALIDATION" for e in self.engine.s["outbox"]),before)
        engine=Engine(self.data,Policy())
        engine.run()
        engine.s["status"]="RUNNING"
        mark=deepcopy(self.data["events"][-2])
        mark["id"]="repeat-summary"
        mark["at"]="2026-04-16T20:04:00+09:00"
        engine.process(mark)
        self.assertEqual(sum(e["kind"]=="EVENING_SUMMARY" for e in engine.s["outbox"]),3)

    def test_failure_is_distinct_from_pause(self):
        self.until("REPORT")
        self.engine.process({"id":"fatal", "type":"ERROR", "at":"2026-04-13T08:31:00+09:00","fatal":True,"reason":"calculation failure"})
        self.assertEqual(self.engine.s["status"],"FAILED")
        self.assertFalse(self.engine.s["pending"])
        self.assertEqual(self.engine.s["outbox"][-1]["kind"],"ERROR")

    def test_transaction_rolls_back_failed_step(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/"paper.sqlite3")
            eid=store.create(self.data,Policy())
            store.apply(eid,"start")
            store.apply(eid,limit=1)
            before=store.get(eid)
            with self.assertRaises(ValueError):
                store.apply(eid,"resume")
            self.assertEqual(store.get(eid),before)

    def test_financial_only_invalidation_and_sma_updates(self):
        self.until("OPEN")
        event=deepcopy(next(e for e in self.data["events"] if e["type"]=="CHECK"))
        event["financial"]["ttm_operating_income"]="-1"
        self.engine.process(event)
        self.assertEqual(self.engine.s["pending"]["900001"]["side"],"SELL")
        self.assertEqual(self.engine.s["invalidated"]["900001"]["causes"][0]["code"],"GENERAL_FINANCIAL")
        event["price"]["closes"]=["100"]*19+["101"]
        result=invalidation({"kind":"GENERAL"},event)[0]
        self.assertEqual(result["sma20"],"100.05")

    def test_new_data_must_resolve_every_invalidation_cause(self):
        self.until("OPEN")
        event=deepcopy(next(e for e in self.data["events"] if e["type"]=="CHECK"))
        event["price"]["closes"]=["100"]*19+["50"]
        event["financial"]["ocf"]="0"
        self.engine.process(event)
        next_open=next(e for e in self.data["events"] if e["type"]=="OPEN" and e["at"].startswith("2026-04-14"))
        self.engine.process(next_open)
        resolved=deepcopy(event)
        resolved["id"]="partial-resolve"
        resolved["at"]="2026-04-14T10:00:00+09:00"
        resolved["observed_at"]=resolved["at"]
        resolved["price"]["closes"]=["100"]*20
        for name in ("price","financial"):
            resolved[name]["id"]+="-new"
            for field in ("published_at","observed_at","available_at"):
                resolved[name][field]=resolved["at"]
        self.engine.process(resolved)
        self.assertFalse(self.engine.reentry_allowed("900001",resolved))

    def test_dataset_rejects_duplicate_naive_and_nonfinite_prices(self):
        data=deepcopy(self.data)
        data["events"][1]["id"]=data["events"][0]["id"]
        with self.assertRaises(ValueError):validate_dataset(data)
        data=deepcopy(self.data)
        data["events"][1]["prices"]["900001"]="NaN"
        with self.assertRaises(ValueError):validate_dataset(data)
        data=deepcopy(self.data)
        data["events"][0]["at"]="2026-04-13T08:30:00"
        with self.assertRaises(ValueError):validate_dataset(data)

from decimal import Decimal
if __name__ == "__main__":
    unittest.main()

