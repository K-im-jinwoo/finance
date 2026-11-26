from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from stock_assistant.backtest import SWING_POLICY
from stock_assistant.models import OHLCV, to_json_value
from stock_assistant.performance import PerformanceRecord, PerformanceStatus
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData

from research_dashboard.research_bridge import (
    get_research_chart,
    get_research_report,
    list_research_reports,
)
from research_dashboard.swing_bridge import get_performance, get_swing_report, list_swing_reports


AS_OF = datetime(2025, 10, 2, 7, tzinfo=timezone.utc)
SESSION = date(2025, 10, 1)
UR = "UR-" + "a" * 24
OLD_UR = "UR-" + "b" * 24
SW = "SW-" + "c" * 24
REGULAR = "R-20251002T0700Z-AB12CD34"
SYMBOL = "005930"


class ResearchSwingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stock_path = self.root / ".runtime" / "stock-assistant.sqlite3"
        self.research_path = self.root / ".runtime" / "unified-research-inputs.sqlite3"
        env = patch.dict("os.environ", {"STOCK_DATABASE_PATH": "", "STOCK_UNIFIED_RESEARCH_DATABASE": ""})
        env.start()
        self.addCleanup(env.stop)

    def seed(self):
        repository = StockRepository(self.stock_path)
        research = ResearchData(self.research_path, source_database=self.stock_path)
        return repository, research

    def publish_research(self, store, report_id=UR, *, as_of=AS_OF, candidate=None, observed_at=None):
        if candidate is None:
            candidate = {
                "symbol": SYMBOL,
                "name": "fixture issuer",
                "action_decision": "WATCH",
                "financial_score": "84.5",
                "news_score": "65",
                "risk_penalty": "5",
                "total_score": "73.65",
                "financial_breakdown": {"profit": {"points": 8, "maximum": 12, "status": "SCORED"}},
                "price": {"session": SESSION.isoformat(), "strategy": "MOMENTUM_CONTINUATION",
                          "entry_conditions": ["stored entry condition"], "credential": "private-credential"},
                "advisories": ["stored advisory"],
                "news_events": [{"event_id": "fixture", "points": 15, "status": "VERIFIED_ACTIVE",
                                 "unrecognized_secret": "private-credential"}],
                "recent_headlines": [
                    {"title": "verified event", "published_at": as_of.isoformat(), "url": "https://example.invalid/news/1"},
                    {"title": "unsafe link", "url": "file:///private/path"},
                ],
                "unknown_candidate_field": "private-credential",
            }
        report = {
            "report_id": report_id, "contract": "UNIFIED_RESEARCH_V1", "policy_version": "TEST_POLICY",
            "as_of": as_of.isoformat(), "expected_price_session": SESSION.isoformat(),
            "candidates": [candidate], "evaluations": [candidate, {
                "symbol": "000660", "name": "excluded issuer", "action_decision": "EXCLUDED",
            }],
            "decision_counts": {"WATCH": 1, "EXCLUDED": 1},
            "unknown_report_field": "private-credential",
            "mode": "SYNTHETIC" if report_id == UR else "MALFORMED_MODE",
        }
        store.put("unified_report", "ALL", report, observed_at=observed_at or as_of)

    def publish_swing(self, store):
        store.publish_swing_report(SESSION, {
            "report_id": SW, "contract": SWING_POLICY, "policy_version": SWING_POLICY,
            "as_of": AS_OF.isoformat(), "price_session": SESSION.isoformat(),
            "candidates": [
                {"symbol": SYMBOL, "name": "fixture issuer", "turnover_multiple": "2.75",
                 "news_evidence": [{"title": "contract", "category": "CONTRACT",
                                    "url": "https://example.invalid/article?n=1",
                                    "excerpt": "saved excerpt", "published_at": AS_OF.isoformat(),
                                    "credentials": "private-credential"}]},
                {"symbol": "000660", "name": "classified issuer", "hermes_duration": "ONE_OFF",
                 "hermes_evidence_index": 0, "news_evidence": [
                    {"title": "earnings", "category": "EARNINGS", "url": "https://example.invalid/article?n=2"}]},
            ],
            "status": "READY", "news_checked_count": 12, "mode": "OBSERVED",
        }, observed_at=AS_OF)

    def bars(self, count):
        bars = []
        for index in range(count):
            day = SESSION - timedelta(days=count - 1 - index)
            observed = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=8)
            close = Decimal(100 + index)
            bars.append(OHLCV(SYMBOL, day, close, close + 2, close - 2, close, 100, "FIXTURE", observed))
        return bars

    def record(self, report_id, days, status, *, symbol=SYMBOL, net=None):
        return PerformanceRecord(
            report_id=report_id, ruleset_version="TEST_POLICY", symbol=symbol, decision="WATCH",
            strategy="TEST_SETUP", expected_holding_period="fixture", score=Decimal("84"),
            signal_at=AS_OF, evaluated_at=AS_OF + timedelta(days=75), trading_days=days,
            status=status, entry_date=None, entry_price=None, exit_date=None,
            gross_return=net + Decimal("0.003") if net is not None else None, net_return=net,
        )

    def test_missing_databases_do_not_create_runtime(self):
        absent = {"present": False, "reason": "DATABASE_MISSING"}
        self.assertEqual(list_research_reports(self.root), absent)
        self.assertEqual(get_research_report(self.root, UR), absent)
        self.assertEqual(get_research_chart(self.root, SYMBOL, UR), absent)
        self.assertEqual(list_swing_reports(self.root), absent)
        self.assertEqual(get_swing_report(self.root, SW), absent)
        self.assertEqual(get_performance(self.root), absent)
        self.assertFalse((self.root / ".runtime").exists())

    def test_wrong_schema_and_corrupt_database_yield_safe_reason(self):
        self.stock_path.parent.mkdir()
        self.stock_path.write_bytes(b"invalid sqlite private path")
        with closing(sqlite3.connect(self.research_path)) as db, db:
            db.execute("CREATE TABLE unrelated (secret TEXT)")
        absent = {"present": False, "reason": "DATABASE_UNAVAILABLE"}
        self.assertEqual(list_research_reports(self.root), absent)
        self.assertEqual(get_swing_report(self.root, SW), absent)
        self.assertEqual(get_performance(self.root), absent)

    def test_invalid_report_id_or_symbol_precedes_database_access(self):
        self.assertEqual(get_research_report(self.root, "../private")["reason"], "INVALID_REPORT_ID")
        self.assertEqual(get_swing_report(self.root, UR)["reason"], "INVALID_REPORT_ID")
        self.assertEqual(get_performance(self.root, "R-injected'")["reason"], "INVALID_REPORT_ID")
        self.assertEqual(get_research_chart(self.root, "００５９３０", UR)["reason"], "INVALID_SYMBOL")
        self.assertEqual(get_research_chart(self.root, "0007A0", UR)["reason"], "INVALID_SYMBOL")
        self.assertEqual(get_research_chart(self.root, SYMBOL, None)["reason"], "INVALID_REPORT_ID")

    def test_research_list_and_detail_preserve_observed_snapshot(self):
        _, research = self.seed()
        self.publish_research(research, OLD_UR, as_of=AS_OF - timedelta(days=1))
        self.publish_research(research)
        self.assertEqual([item["report_id"] for item in list_research_reports(self.root)["reports"]], [UR, OLD_UR])
        summary = list_research_reports(self.root)["latest"]
        self.assertEqual((summary["report_id"], summary["candidate_count"], summary["decision_counts"]["EXCLUDED"]),
                         (UR, 1, 1))
        self.assertEqual(summary["observed_at"], AS_OF.isoformat())
        self.assertEqual(summary["mode"], "SYNTHETIC")
        self.assertNotIn("mode", list_research_reports(self.root)["reports"][1])
        self.assertNotIn("mode", get_research_report(self.root, OLD_UR)["report"])
        report = get_research_report(self.root, UR)["report"]
        self.assertEqual([item["action_decision"] for item in report["evaluations"]], ["WATCH", "EXCLUDED"])
        self.assertEqual(report["mode"], "SYNTHETIC")
        candidate = report["candidates"][0]
        self.assertEqual(candidate["financial_score"], "84.5")
        self.assertEqual(candidate["financial_breakdown"]["profit"]["points"], 8)
        self.assertEqual(candidate["price"]["entry_conditions"], ["stored entry condition"])
        self.assertEqual(candidate["news_events"][0]["points"], 15)
        self.assertEqual(candidate["recent_headlines"][0]["url"], "https://example.invalid/news/1")
        self.assertIsNone(candidate["recent_headlines"][1]["url"])
        self.assertNotIn("private-credential", json.dumps(report))
        self.assertEqual(get_research_report(self.root, "UR-" + "d" * 24)["reason"], "REPORT_NOT_FOUND")

    def test_chart_pins_both_session_and_observation_cutoffs(self):
        stock, research = self.seed()
        bars = self.bars(120)
        fingerprint = hashlib.sha256(json.dumps(to_json_value(bars), sort_keys=True).encode()).hexdigest()
        self.publish_research(research, candidate={
            "symbol": SYMBOL, "price": {"strategy": "MOMENTUM_CONTINUATION", "session": SESSION.isoformat()},
            "price_input_fingerprint": fingerprint,
        })
        correction = OHLCV(SYMBOL, SESSION, Decimal(500), Decimal(501), Decimal(499),
                           Decimal(500), 200, "FIXTURE", AS_OF + timedelta(seconds=1))
        newer_session = OHLCV(SYMBOL, AS_OF.date(), Decimal(600), Decimal(601), Decimal(599),
                              Decimal(600), 300, "FIXTURE", AS_OF - timedelta(hours=1))
        stock.save_bars([*bars, correction, newer_session])
        result = get_research_chart(self.root, SYMBOL, UR)
        self.assertTrue(result["present"])
        self.assertEqual(len(result["bars"]), 120)
        self.assertEqual(result["price_session"], "2025-10-01")
        self.assertEqual(result["bars"][-1]["close"], 219)
        self.assertEqual(result["bars"][-1]["sma120"], 159.5)
        self.assertEqual(result["bars"][-1]["volume_ratio"], 1)
        self.assertIsNone(result["bars"][13]["atr14"])
        self.assertIsNone(result["bars"][19]["volume_ratio"])
        self.assertEqual(result["bars"][-1]["atr14"], 4)
        self.assertEqual(result["bars"][-1]["rsi14"], 100)
        self.assertEqual(result["analysis"]["session"], "2025-10-01")
        self.assertEqual(result["technical"]["stored_pattern"]["code"], "MOMENTUM_CONTINUATION")
        self.assertTrue(result["analysis_available"])

    def test_extended_sma_history_populates_visible_trace_without_changing_report_analysis(self):
        stock, research = self.seed()
        history = self.bars(245)
        candidate_window = history[-120:]
        fingerprint = hashlib.sha256(json.dumps(to_json_value(candidate_window), sort_keys=True).encode()).hexdigest()
        self.publish_research(research, candidate={"symbol": SYMBOL, "price_input_fingerprint": fingerprint})
        warmup_correction = OHLCV(SYMBOL, history[6].trade_date, Decimal(800), Decimal(801), Decimal(799),
                                  Decimal(800), 100, "FIXTURE", AS_OF + timedelta(seconds=1))
        stock.save_bars([*history, warmup_correction])

        result = get_research_chart(self.root, SYMBOL, UR)
        self.assertTrue(result["present"])
        self.assertEqual(len(result["bars"]), 120)
        first, last = result["bars"][0], result["bars"][-1]
        self.assertEqual(first["date"], candidate_window[0].trade_date.isoformat())
        self.assertEqual(first["close"], 225)
        self.assertEqual((first["sma5"], first["sma20"], first["sma60"], first["sma120"]),
                         (223, 215.5, 195.5, 165.5))
        self.assertEqual(last["sma120"], 284.5)
        self.assertTrue(all(bar["sma120"] is not None for bar in result["bars"]))
        self.assertEqual(result["analysis"]["bars_count"], 120)
        self.assertIsNone(result["analysis"]["moving_averages"]["series"][120][0])
        self.assertIsNone(first["atr14"])
        self.assertIsNone(first["rsi14"])

    def test_invalid_older_warmup_bar_leaves_visible_chart_and_sma_gap(self):
        stock, research = self.seed()
        history = self.bars(125)
        bad = history[2]
        zero_price = Decimal(0)
        history[2] = OHLCV(SYMBOL, bad.trade_date, zero_price, zero_price, zero_price, zero_price,
                           0, "FIXTURE", bad.observed_at)
        stock.save_bars(history)
        self.publish_research(research)

        result = get_research_chart(self.root, SYMBOL, UR)
        self.assertTrue(result["present"])
        self.assertEqual(len(result["bars"]), 120)
        self.assertIsNone(result["bars"][0]["sma5"])
        self.assertEqual(result["bars"][2]["sma5"], 105)
        self.assertIsNone(result["bars"][116]["sma120"])
        self.assertEqual(result["bars"][117]["sma120"], 162.5)

    def test_short_history_has_chart_values_with_indicator_warmup_and_no_full_analysis(self):
        stock, research = self.seed()
        stock.save_bars(self.bars(60))
        self.publish_research(research)
        result = get_research_chart(self.root, SYMBOL, UR)
        self.assertTrue(result["present"])
        self.assertIsNone(result["bars"][3]["sma5"])
        self.assertEqual(result["bars"][4]["sma5"], 102)
        self.assertIsNone(result["bars"][-1]["atr14"])
        self.assertIsNone(result["bars"][-1]["volume_ratio"])
        self.assertIsNone(result["bars"][-1]["rsi14"])
        self.assertEqual(result["bars"][-1]["sma60"], 129.5)
        self.assertIsNone(result["analysis"])
        self.assertIsNone(result["technical"])
        self.assertFalse(result["analysis_available"])

    def test_chart_reports_missing_data_and_fingerprint_mismatch(self):
        stock, research = self.seed()
        self.publish_research(research, candidate={"symbol": SYMBOL, "price_input_fingerprint": "0" * 64})
        self.assertEqual(get_research_chart(self.root, "999999", UR)["reason"], "SYMBOL_NOT_IN_REPORT")
        self.assertEqual(get_research_chart(self.root, SYMBOL, UR)["reason"], "BARS_NOT_FOUND")
        stock.save_bars(self.bars(61))
        self.assertEqual(get_research_chart(self.root, SYMBOL, UR)["reason"], "REPORT_PRICE_INPUT_MISMATCH")

    def test_swing_reports_preserve_safe_evidence_and_stored_classification(self):
        _, research = self.seed()
        self.publish_swing(research)
        summary = list_swing_reports(self.root)["latest"]
        self.assertEqual((summary["report_id"], summary["candidate_count"], summary["price_session"]),
                         (SW, 2, SESSION.isoformat()))
        self.assertEqual(summary["mode"], "OBSERVED")
        report = get_swing_report(self.root, SW)["report"]
        self.assertEqual(report["mode"], "OBSERVED")
        candidates = report["candidates"]
        self.assertEqual(candidates[0]["news_evidence"][0]["excerpt"], "saved excerpt")
        self.assertEqual(candidates[0]["news_evidence"][0]["category"], "CONTRACT")
        self.assertIsNone(candidates[0]["hermes_duration"])
        self.assertEqual(candidates[1]["hermes_duration"], "ONE_OFF")
        self.assertEqual(candidates[1]["hermes_evidence_index"], 0)
        self.assertNotIn("private-credential", json.dumps(candidates))

    def test_performance_uses_saved_records_and_native_complete_only_summary(self):
        stock, _ = self.seed()
        stock.save_performance_records([
            self.record(UR, 5, PerformanceStatus.COMPLETE, net=Decimal("0.02")),
            self.record(UR, 5, PerformanceStatus.COMPLETE, symbol="000660", net=Decimal("-0.01")),
            self.record(UR, 20, PerformanceStatus.PENDING_HORIZON),
            self.record(SW, 3, PerformanceStatus.PENDING_ENTRY),
            self.record(REGULAR, 60, PerformanceStatus.UNTRADABLE_ENTRY),
        ])
        result = get_performance(self.root, UR)
        self.assertTrue(result["present"])
        self.assertEqual({record["report_id"] for record in result["records"]}, {UR})
        self.assertEqual({record["trading_days"] for record in result["records"]}, {5, 20})
        self.assertEqual(result["summary"], [{
            "trading_days": 5, "sample_count": 2, "average_net_return": "0.005",
            "win_rate": "0.5", "ruleset_version": "TEST_POLICY", "decision": "WATCH", "strategy": "TEST_SETUP",
        }])
        all_records = get_performance(self.root)["records"]
        self.assertEqual({record["report_id"] for record in all_records}, {UR, SW, REGULAR})
        self.assertEqual(get_performance(self.root, "UR-" + "d" * 24)["records"], [])

    def test_empty_research_database_and_missing_stock_data_are_independent(self):
        _, research = self.seed()
        self.assertEqual(list_research_reports(self.root), {"present": True, "reports": [], "latest": None})
        self.publish_research(research)
        self.stock_path.unlink()
        self.assertTrue(get_research_report(self.root, UR)["present"])
        self.assertEqual(get_research_chart(self.root, SYMBOL, UR)["reason"], "DATABASE_MISSING")

    def test_research_evidence_urls_and_refresh_errors_do_not_expose_credentials(self):
        _, research = self.seed()
        report = {
            "report_id": UR, "contract": "UNIFIED_RESEARCH_V1", "policy_version": "TEST_POLICY",
            "as_of": AS_OF.isoformat(),
            "refresh_stages": {
                "news_budget": {"reserved_calls": 20, "actual_calls": 9},
                "prices": {"status": "UNAVAILABLE", "error_code": "Unable to open C:/private/path"},
            },
            "research_windows": {"official_start": "2015-10-01"},
            "candidates": [{
                "symbol": SYMBOL,
                "pending_checks": ["MATERIAL_EVENT_REVIEW_REQUIRED:https://example.invalid/a?api_key=secret-test"],
                "recent_headlines": [{"title": "reference", "url": "https://example.invalid/a?access_token=secret-test"}],
            }],
        }
        research.put("unified_report", "ALL", report, observed_at=AS_OF)
        observed = get_research_report(self.root, UR)["report"]
        self.assertEqual(observed["research_windows"]["official_start"], "2015-10-01")
        self.assertEqual(observed["refresh_stages"]["news_budget"]["actual_calls"], 9)
        self.assertEqual(observed["refresh_stages"]["prices"]["status"], "UNAVAILABLE")
        self.assertEqual(observed["candidates"][0]["pending_checks"], ["MATERIAL_EVENT_REVIEW_REQUIRED"])
        self.assertNotIn("secret-test", json.dumps(observed))
        self.assertNotIn("C:/private/path", json.dumps(observed))

    def test_alternate_database_paths_are_read_without_writes(self):
        stock, research = self.seed()
        self.publish_research(research)
        self.publish_swing(research)
        stock.save_bars(self.bars(61))
        sources = (self.stock_path, self.research_path)
        snapshots = [(item.read_bytes(), item.stat().st_mtime_ns) for item in sources]
        members = set(self.stock_path.parent.iterdir())
        other_root = self.root / "alternate"
        with patch.dict("os.environ", {"STOCK_DATABASE_PATH": str(self.stock_path),
                                      "STOCK_UNIFIED_RESEARCH_DATABASE": str(self.research_path)}):
            self.assertTrue(get_research_chart(other_root, SYMBOL, UR)["present"])
            self.assertTrue(list_research_reports(other_root)["present"])
            self.assertTrue(list_swing_reports(other_root)["present"])
            self.assertTrue(get_swing_report(other_root, SW)["present"])
            self.assertTrue(get_performance(other_root)["present"])
        self.assertFalse(other_root.exists())
        self.assertEqual([(item.read_bytes(), item.stat().st_mtime_ns) for item in sources], snapshots)
        self.assertEqual(set(self.stock_path.parent.iterdir()), members)
