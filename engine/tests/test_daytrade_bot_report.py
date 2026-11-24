"""Offline end-to-end accounting, scheduling and CLI contract checks."""
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from stock_assistant.daytrade import run_paper_session, build_trading_report, render_markdown_report
from stock_assistant.daytrade.bot import scan_dataset
from stock_assistant.daytrade.ledger import DaytradeLedger
from stock_assistant.daytrade.report import _drawdown
from stock_assistant.daytrade.stream import LivePaperSession

FIXTURE = Path(__file__).parent / "fixtures/daytrade_bot_sample.json"
ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"


def dataset():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def event_named(data, name):
    return next(e for e in data["events"] if e["id"] == name)


class BotReportTests(unittest.TestCase):
    def run_session(self, data=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        report = run_paper_session(data or dataset(), temporary.name)
        return report, Path(temporary.name)

    def test_sample_complete_pipeline_literal_net_profit_and_reopened_report(self):
        report, root = self.run_session()
        account = report["account"]
        self.assertEqual(Decimal(account["cash"]), Decimal("10091159.2040"))
        self.assertEqual(Decimal(account["realized_pnl_net"]), Decimal("91159.2040"))
        self.assertEqual(account["cash"], account["final_equity"])
        self.assertEqual(account["net_pnl"], account["realized_pnl_net"])
        self.assertEqual(account["completed_trades"], 2)
        self.assertEqual(Decimal(account["win_rate_pct"]), Decimal(50))
        self.assertEqual(Decimal(account["mdd_pct"]), Decimal("0.17148076"))
        self.assertEqual(report["sessions"], {"2026-11-13": "CLOSED"})
        self.assertEqual([f["side"] for f in report["fills"]], ["BUY", "BUY", "SELL", "SELL"])
        self.assertEqual({f["signal"]["exit_reason"] for f in report["fills"] if f["side"] == "SELL"},
                         {"TAKE_PROFIT", "TIME_EXIT"})
        self.assertTrue(any("ENTRY_NOT_ALLOWED" in s["reasons"] for s in report["signals"]
                            if s["at"].startswith("2026-11-13T05:50")))
        excluded = {r["symbol"]: r["reason"] for r in report["scan_history"][0]["rejected"]}
        self.assertEqual(excluded, {"035420": "HALTED", "035720": "LOW_TURNOVER"})
        ledger = DaytradeLedger(root)
        try:
            before = ledger.path.read_bytes()
            self.assertTrue(ledger.audit()["cash_identity"])
            self.assertEqual(report, build_trading_report(ledger))
            self.assertEqual(before, ledger.path.read_bytes())
        finally:
            ledger.close()
        json.dumps(report, allow_nan=False)
        text = render_markdown_report(report)
        for expected in ("KRX+NXT", "orders_enabled=false", "실현 손익", "TAKE_PROFIT", "TIME_EXIT", "ENTRY_CLOSED", "CLOSED"):
            self.assertIn(expected, text)

    def test_sample_repeated_run_has_same_hash_and_duplicate_inputs_have_no_extra_fills(self):
        first, _ = self.run_session()
        second, _ = self.run_session()
        self.assertEqual(first, second)
        data = dataset()
        data["events"].append(deepcopy(event_named(data, "buy-profit")))
        duplicate, _ = self.run_session(data)
        self.assertEqual(duplicate["account"], first["account"])
        self.assertEqual(duplicate["fills"], first["fills"])

    def test_no_new_book_no_fabricated_fill_and_unflattened_unknown_valuation(self):
        data = dataset()
        data["events"] = [e for e in data["events"] if e["id"] != "time-exit-fill"]
        report, _ = self.run_session(data)
        self.assertEqual(report["sessions"], {"2026-11-13": "UNFLATTENED"})
        self.assertIsNone(report["account"]["final_equity"])
        self.assertIsNone(report["account"]["net_pnl"])
        self.assertIsNone(report["account"]["mdd_pct"])
        self.assertGreater(Decimal(report["account"]["valuation_missing_pct"]), 0)
        markdown = render_markdown_report(report)
        self.assertIn("평가점 누락률(%)", markdown)
        self.assertIn("관측 구간 낙폭(%)", markdown)
        self.assertEqual(report["account"]["valuation_status"], "MARK_UNCERTAIN")
        self.assertEqual(report["account"]["completed_trades"], 1)
        self.assertTrue(report["audit"]["quantity_identity"])
        self.assertIn("MARK_UNCERTAIN", render_markdown_report(report))

    def test_stop_loss_and_daily_loss_exit_are_costed_actual_fills(self):
        for bid, expected in (("10500", "STOP_LOSS"), ("8000", "LOSS_EXIT")):
            data = dataset()
            for name in ("take-profit-signal", "take-profit-fill"):
                payload = event_named(data, name)["data"]["payload"]
                payload["bids"][0]["price"] = bid
                payload["asks"][0]["price"] = str(int(bid) + 10)
            report, _ = self.run_session(data)
            self.assertIn(expected, [s["exit_reason"] for s in report["signals"]])
            self.assertTrue(any(f["signal"]["exit_reason"] == expected for f in report["fills"] if f["side"] == "SELL"))
            self.assertTrue(report["audit"]["cash_identity"])
            if expected == "LOSS_EXIT":
                self.assertTrue(any("LOSS_LOCKED" in r["locks"] for r in report["risk_events"]))

    def test_recorded_data_source_is_independent_of_simulated_execution(self):
        with tempfile.TemporaryDirectory() as root, LivePaperSession(root, websocket_slot_available=True, stream_kind="RECORDED") as session:
            report = session.report()
        self.assertEqual(report["data_source"], "LIVE_RECORDED")
        self.assertEqual(report["execution_mode"], "SIMULATED_FILL")
        self.assertEqual(report["dataset_kind"], "LIVE_RECORDED")
        self.assertIn("LIVE_RECORDED", render_markdown_report(report))

    def test_missing_rate_includes_known_compacted_points_and_drawdown_uses_known_values(self):
        curve = [{"equity": None, "diagnostic_truncation": True,
                  "omitted_valuation_points": 4, "omitted_missing_valuation_points": 2},
                 {"equity": "120"}, {"equity": None}, {"equity": "90"}]
        result = _drawdown(curve, Decimal(100))
        self.assertIsNone(result["mdd_pct"])
        self.assertEqual(result["mdd_observed_pct"], Decimal(25))
        self.assertEqual(result["valuation_missing_pct"], Decimal(300) / 7)

    def test_clock_requests_time_exit_and_next_post_latency_book_fills(self):
        data = dataset()
        e = event_named(data, "time-exit-signal")
        e.update(kind="CLOCK", data={})
        report, _ = self.run_session(data)
        time_signals = [s for s in report["signals"] if s["exit_reason"] == "TIME_EXIT"]
        self.assertIsNone(time_signals[0]["reference_price"])
        self.assertEqual(report["sessions"]["2026-11-13"], "CLOSED")

    def test_partial_fills_cancel_remaining_buy_before_exiting(self):
        data = dataset()
        data["fixture_costs"]["participation"] = "0.001"
        report, root = self.run_session(data)
        self.assertTrue(any(i["status"] == "PARTIAL" for i in report["decisions"]))
        self.assertTrue(any(i["reason"] == "BUY_REMAINDER_CANCELLED_FOR_EXIT" for i in report["decisions"]))
        self.assertTrue(all(f["quantity"] == 1 for f in report["fills"]))
        quotes = [f["quote_event_id"] for f in report["fills"]]
        self.assertEqual(len(quotes), len(set(quotes)))
        ledger = DaytradeLedger(root)
        try:
            self.assertTrue(ledger.audit()["quantity_identity"])
        finally:
            ledger.close()

    def test_latency_zero_depth_and_price_gap_do_not_force_a_fill(self):
        for change in ("latency", "zero-depth", "price-gap"):
            data = dataset()
            if change == "latency":
                data["fixture_costs"]["latency_ms"] = 100000
            elif change == "zero-depth":
                event_named(data, "buy-profit")["data"]["payload"]["asks"][0]["volume"] = "0"
            else:
                for side, price in (("bids", "19990"), ("asks", "20000")):
                    event_named(data, "buy-profit")["data"]["payload"][side][0]["price"] = price
            report, _ = self.run_session(data)
            self.assertTrue(report["audit"]["cash_identity"])
            self.assertTrue(any(i["status"] in {"SKIPPED", "REJECTED"} for i in report["decisions"]))
            if change == "latency":
                self.assertFalse(report["fills"])

    def test_read_only_scan_and_input_rejection_before_database_creation(self):
        result = scan_dataset(dataset())
        self.assertEqual({r["symbol"] for r in result.selected}, {"005930", "000660"})
        for mutate in ("kind", "sequence", "conflict", "fill", "policy"):
            data = dataset()
            if mutate == "kind": data["dataset_kind"] = "OBSERVATION"
            elif mutate == "sequence": data["events"][1]["sequence"] = 0
            elif mutate == "conflict": data["events"].append(data["events"][0] | {"data": {}})
            elif mutate == "fill": data["events"][1]["kind"] = "FILL"
            else: data["policy_version"] = "PAPER_READY"
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ValueError): run_paper_session(data, folder)
                self.assertFalse((Path(folder) / ".runtime/intraday-paper.sqlite3").exists())

    def test_existing_ledger_is_never_overwritten(self):
        _, root = self.run_session()
        path = root / ".runtime/intraday-paper.sqlite3"
        before = path.read_bytes()
        with self.assertRaises(FileExistsError): run_paper_session(dataset(), root)
        self.assertEqual(before, path.read_bytes())

    def test_pending_buy_reservations_limit_three_names_before_any_fill(self):
        data = dataset()
        first = data["events"][:5]
        extras = []
        for code in ("010950", "051910"):
            for source in ("initial-book-005930", "candles-005930"):
                event = deepcopy(event_named(data, source))
                event["id"] = source + "-" + code
                event["data"]["symbol"] = code
                event["data"]["received_monotonic"] += len(first) + len(extras)
                extras.append(event)
        ranked = event_named(data, "rankings")
        rows = ranked["data"]["turnover"]["rankings"]
        rows.extend([rows[0] | {"symbol": code, "rank": i, "tradingAmount": amount}
                     for i, code, amount in ((5, "010950", "2000000000"), (6, "051910", "1000000000"))])
        data["events"] = first + extras + [ranked]
        for i, event in enumerate(data["events"]):
            event["sequence"] = i
            if event["kind"] == "MARKET": event["data"]["received_monotonic"] = i
        report, _ = self.run_session(data)
        self.assertEqual(len(report["intents"]), 3)
        self.assertFalse(report["fills"])
        self.assertTrue(any(d["reason"] == "CAPITAL_OR_POSITION_LIMIT" for d in report["decisions"]))
        total = sum(i[1]["quantity"] for i in report["intents"])
        self.assertLessEqual(Decimal(total) * Decimal("11002.2") * Decimal("1.0005"), Decimal("6000000"))

    def test_expired_or_newly_excluded_candidate_cancels_pending_buy(self):
        data = dataset()
        prefix = data["events"][:6]
        late = deepcopy(event_named(data, "buy-profit"))
        late["at"] = "2026-11-13T10:04:32+09:00"
        late["data"]["payload"]["timestamp"] = late["at"]
        data["events"] = prefix + [late]
        report, _ = self.run_session(data)
        self.assertFalse(report["fills"])
        self.assertTrue(any(d["reason"] == "BUY_REMAINDER_CANDIDATE_UNAVAILABLE" for d in report["decisions"]))
        data = dataset()
        rerank = deepcopy(event_named(data, "rankings"))
        rerank["id"] = "adverse-rankings"
        rerank["data"]["risk_meta"]["005930"] = {"halted": True}
        data["events"].insert(6, rerank)
        for i, event in enumerate(data["events"]): event["sequence"] = i
        report, _ = self.run_session(data)
        self.assertFalse(any(f["symbol"] == "005930" for f in report["fills"]))
        self.assertTrue(any(d["reason"] == "CANDIDATE_REMOVED" for d in report["decisions"]))


class BotCliTests(unittest.TestCase):
    def cli(self, *args):
        environment = os.environ | {"PYTHONPATH": str(ENGINE_SRC), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-m", "stock_assistant.daytrade", *map(str, args)],
            capture_output=True, encoding="utf-8", env=environment, timeout=30)

    def test_cli_scan_quotes_simulate_and_reopened_json_markdown_reports(self):
        scanned = self.cli("scan", FIXTURE)
        self.assertEqual(scanned.returncode, 0, scanned.stdout + scanned.stderr)
        self.assertEqual(len(json.loads(scanned.stdout)["selected"]), 2)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "session"
            md, js = Path(folder) / "report.md", Path(folder) / "report.json"
            result = self.cli("simulate", FIXTURE, "--root", root, "--report-md", md, "--report-json", js)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report, json.loads(js.read_text(encoding="utf-8")))
            self.assertEqual(md.read_text(encoding="utf-8"), render_markdown_report(report))
            self.assertFalse(md.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue((root / ".runtime/intraday-paper.sqlite3").is_file())
            reopened = self.cli("report", "--root", root, "--format", "json")
            self.assertEqual(reopened.returncode, 0, reopened.stdout + reopened.stderr)
            self.assertEqual(json.loads(reopened.stdout), report)
            markdown = self.cli("report", "--root", root)
            self.assertEqual(markdown.returncode, 0)
            self.assertIn("모의거래 리포트", markdown.stdout)
            quotes = Path(folder) / "quotes.json"
            payloads = {e["data"]["symbol"]: e["data"]["payload"] for e in dataset()["events"]
                if e["id"].startswith("initial-book-")}
            quotes.write_text(json.dumps(payloads), encoding="utf-8")
            scanned = self.cli("scan", FIXTURE, "--quotes", quotes)
            self.assertEqual(scanned.returncode, 0, scanned.stdout + scanned.stderr)
            self.assertFalse(json.loads(scanned.stdout)["selected"][0]["entry_blocked"])

    def test_cli_does_not_overwrite_report_and_invalid_input_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "session"
            output = Path(folder) / "existing.md"
            output.write_text("preserve", encoding="utf-8")
            failed = self.cli("simulate", FIXTURE, "--root", root, "--report-md", output)
            self.assertEqual(failed.returncode, 1)
            self.assertEqual(output.read_text(), "preserve")
            self.assertFalse(root.exists())
            bad = Path(folder) / "bad.json"
            bad.write_text('{"dataset_kind":"OBSERVATION"}', encoding="utf-8")
            failed = self.cli("simulate", bad, "--root", root)
            self.assertEqual(failed.returncode, 1)
            self.assertNotIn("Traceback", failed.stderr + failed.stdout)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
