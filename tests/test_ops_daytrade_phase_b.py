"""Read-only operations and daytrade history using isolated fixtures."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_dashboard import daytrade_bridge as daytrade
from research_dashboard import ops_bridge as ops
from stock_assistant.daytrade.daemon import open_or_resume_session
from stock_assistant.daytrade.ledger import DaytradeLedger
from stock_assistant.models import Decision, Holding, OHLCV, ScreeningResult, ThesisCard, ThesisStatus
from stock_assistant.news_cost import NewsApiBudget
from stock_assistant.repository import StockRepository

AT = datetime.fromisoformat("2026-11-26T09:20:00+09:00")


class OpsDaytradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / ".runtime/daytrade"
        env = patch.dict("os.environ", {
            "DAYTRADE_STATE_ROOT": "",
            "DAYTRADE_TOKEN_CACHE": "",
            "STOCK_DATABASE_PATH": "",
            "STOCK_NEWS_USAGE_FILE": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def day_session(self, day, *, keep_open=False):
        session = open_or_resume_session(self.state, day)
        session.on_session(normal_session_verified=True, at=day + "T09:10:00+09:00")
        if keep_open:
            self.addCleanup(session.close)
            return session
        path = session.ledger.path
        session.close()
        return path

    def assert_unchanged(self, directory, before):
        self.assertEqual(before, {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()})

    def test_dated_reads_use_daily_ledgers_and_safe_reasons(self):
        self.day_session("2026-11-25")
        older = self.day_session("2026-11-24")
        broken = self.state / "daily/2026-11-26/.runtime/intraday-paper.sqlite3"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"private content")
        (self.state / "daily/2026-02-30").mkdir()
        (self.state / "daily/2026-11-23").mkdir()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

        self.assertEqual(daytrade.list_daytrade_sessions(self.root), {
            "present": True, "sessions": ["2026-11-26", "2026-11-25", "2026-11-24"], "latest": "2026-11-26",
        })
        selected = daytrade.read_daytrade_session(self.root, "2026-11-24")
        self.assertTrue(selected["present"])
        self.assertFalse(selected["orders_enabled"])
        self.assertFalse(selected["sample_available"])
        self.assertEqual(selected["report"]["sessions"], {"2026-11-24": "OBSERVING"})
        self.assertEqual(selected["live_settings"]["scanner_config"]["max_watch"], 8)
        for day in ("../2026-11-24", "2026-11-24/../2026-11-25", "2026-02-30", "20261124", ""):
            with self.subTest(day=day):
                self.assertEqual(daytrade.read_daytrade_session(self.root, day)["reason"], "DAYTRADE_DATE_INVALID")
        for day, reason in (("2026-11-27", "DAYTRADE_SESSION_UNAVAILABLE"), ("2026-11-26", "DAYTRADE_REPORT_UNAVAILABLE")):
            with self.subTest(day=day):
                result = daytrade.read_daytrade_session(self.root, day)
                self.assertFalse(result["present"])
                self.assertIsNone(result["report"])
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("private content", json.dumps(result))
        self.assertEqual(before[older], older.read_bytes())
        self.assert_unchanged(self.root, before)

    def test_dated_reads_reject_misfiled_and_unstarted_sessions(self):
        source = self.day_session("2026-11-24")
        incorrect = self.state / "daily/2026-11-25/.runtime/intraday-paper.sqlite3"
        incorrect.parent.mkdir(parents=True)
        shutil.copy2(source, incorrect)
        wrong_day = daytrade.read_daytrade_session(self.root, "2026-11-25")
        self.assertFalse(wrong_day["present"])
        self.assertEqual(wrong_day["reason"], "DAYTRADE_SESSION_DATE_MISMATCH")

        session_root = self.state / "daily/2026-11-26"
        session_root.mkdir()
        DaytradeLedger.create(session_root).close()
        unstarted = daytrade.read_daytrade_session(self.root, "2026-11-26")
        self.assertFalse(unstarted["present"])
        self.assertEqual(unstarted["reason"], "DAYTRADE_SESSION_NOT_RECORDED")

    def test_dated_reads_refuse_links(self):
        self.day_session("2026-11-24")
        daily = self.state / "daily"
        redirected = daily / "2026-11-25"
        source = daily / "2026-11-24"
        try:
            redirected.symlink_to(source, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Windows can exercise directory redirection through a junction
            # when symlink creation requires privileges unavailable to the test.
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(redirected), str(source)],
                                    capture_output=True, timeout=10)
            if result.returncode != 0:
                self.skipTest("Operating system does not permit directory link fixture")
            self.addCleanup(redirected.rmdir)
            self.assertTrue(redirected.is_junction())
        self.assertEqual(daytrade.list_daytrade_sessions(self.root)["sessions"], ["2026-11-24"])
        self.assertFalse(daytrade.read_daytrade_session(self.root, "2026-11-25")["present"])

    def test_dated_reads_refuse_hardlinked_sqlite(self):
        source = self.day_session("2026-11-24")
        linked = self.state / "daily/2026-11-25/.runtime/intraday-paper.sqlite3"
        linked.parent.mkdir(parents=True)
        linked.hardlink_to(source)
        self.assertEqual(daytrade.list_daytrade_sessions(self.root)["sessions"], [])
        self.assertFalse(daytrade.read_daytrade_session(self.root, "2026-11-25")["present"])

    def test_dated_report_tracks_committed_wal_without_caching(self):
        session = self.day_session("2026-11-26", keep_open=True)
        connection = session.ledger.connection
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        self.assertTrue(daytrade.read_daytrade_session(self.root, "2026-11-26")["present"])
        with connection:
            connection.execute("UPDATE meta SET value=? WHERE key='daemon_settings'", (
                json.dumps({"max_daily_entries": 7, "unexpected_secret": "private content"}),
            ))
        result = daytrade.read_daytrade_session(self.root, "2026-11-26")
        self.assertEqual(result["live_settings"], {"max_daily_entries": 7})

    def test_closed_wal_report_does_not_create_sqlite_sidecars(self):
        path = self.day_session("2026-11-26")
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        before = {p: p.read_bytes() for p in path.parent.iterdir()}
        self.assertTrue(daytrade.read_daytrade_session(self.root, "2026-11-26")["present"])
        self.assertEqual(before, {p: p.read_bytes() for p in path.parent.iterdir()})

    def test_ops_missing_sources_do_not_create_files(self):
        result = ops.get_ops_overview(self.root, {}, now=AT)
        self.assertFalse(result["present"])
        self.assertFalse(result["holdings"]["present"])
        self.assertFalse(result["news_budget"]["present"])
        self.assertFalse(result["daytrade"]["present"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_ops_uses_saved_holdings_thesis_and_close_and_never_mutates(self):
        db = self.root / ".runtime/stock-assistant.sqlite3"
        store = StockRepository(db)
        store.replace_holding(Holding("Fixture A", "005930", Decimal(2), Decimal(90)))
        store.replace_holding(Holding("Fixture B", "005930", Decimal(3), Decimal(100)))
        store.replace_holding(Holding("Fixture A", "000660", Decimal(1), Decimal(100)))
        moment = (AT - timedelta(days=1)).astimezone(timezone.utc)
        store.save_thesis(ThesisCard("fixture", "005930", moment,
                                    ("fact",), (), ("condition",), ("check",), ThesisStatus.APPROVED))
        store.save_screening_results("R-20261125T0010Z-1234ABCD", [
            ScreeningResult("005930", moment, Decision.CANDIDATE, Decimal(80), (), (), ("screen check",)),
        ])
        store.save_bars([OHLCV("005930", date(2026, 11, 25), Decimal(105), Decimal(125), Decimal(100),
                              Decimal(120), 1_000, "TEST", moment)])
        before = db.read_bytes()
        result = ops.get_ops_overview(self.root, {}, now=AT)["holdings"]
        self.assertTrue(result["present"])
        self.assertEqual(result["positions"][1], {
            "symbol": "005930", "quantity": "5", "average_price": "96", "brokers": ["Fixture A", "Fixture B"],
        })
        self.assertEqual(result["theses"][0]["status"], "APPROVED")
        self.assertEqual(result["reviews"][0], {"symbol": "000660", "present": False, "reason": "THESIS_UNAVAILABLE"})
        self.assertIn("PARTIAL_PROFIT_REVIEW", result["reviews"][1]["actions"])
        self.assertEqual(result["reviews"][1]["price_date"], "2026-11-25")
        self.assertEqual(db.read_bytes(), before)

    def test_ops_news_cooldown_rollover_and_daemon_whitelist(self):
        self.state.mkdir(parents=True)
        db = self.state.parent / "news-api-usage.sqlite3"
        NewsApiBudget.initialize(db, at=AT - timedelta(minutes=25), used_today=12, used_month=71)
        NewsApiBudget(db, free_check=lambda: None).reserve(3, at=AT - timedelta(minutes=20), scope="SWING")
        daemon = self.state / "status.json"
        daemon.write_text(json.dumps({
            "status": "OBSERVING", "at": AT.isoformat(), "orders_enabled": True, "active_sockets": 1,
            "live_settings": {"max_daily_entries": 5, "strategy_config": {
                "entry_start_hhmm": "09:05", "stop_loss_bps": "150", "token": "private content",
            }}, "deliveries": [{"chat_id": "private content"}], "initial_capital": "10000000",
        }), encoding="utf-8")
        token_cache = self.root / "test-auth"
        token_cache.mkdir()
        token = token_cache / "toss-token.json"
        token.write_text(json.dumps({
            "version": 1, "token": "private content", "client_hash": "f" * 64,
            "valid_until": (AT + timedelta(minutes=8)).timestamp(),
        }), encoding="utf-8")
        token.chmod(0o600)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        verification = {"data_status": "THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY"}
        with patch.dict("os.environ", {"DAYTRADE_TOKEN_CACHE": str(token_cache)}):
            result = ops.get_ops_overview(self.root, verification, now=AT)
            later = ops.get_ops_overview(self.root, verification, now=AT + timedelta(days=1))
        self.assertTrue(result["present"])
        self.assertEqual(result["verification"], verification)
        news = result["news_budget"]
        self.assertEqual((news["daily_used"], news["monthly_used"]), (15, 74))
        self.assertEqual(news["policy_status"], "UNKNOWN")
        self.assertEqual(news["cooldowns"]["SWING"]["status"], "COOLDOWN")
        self.assertEqual((later["news_budget"]["daily_used"], later["news_budget"]["monthly_used"]), (0, 74))
        live = result["daytrade"]
        self.assertEqual(live["status"], "OBSERVING")
        self.assertEqual(live["token_status"], "VALID")
        self.assertEqual(live["token_status_source"], "CACHE_EXPIRY")
        self.assertEqual(later["daytrade"]["token_status"], "EXPIRED")
        self.assertFalse(live["orders_enabled"])
        self.assertEqual(live["live_settings"]["strategy_config"], {"entry_start_hhmm": "09:05", "stop_loss_bps": "150"})
        self.assertNotIn("private content", json.dumps(result))
        self.assertNotIn("f" * 64, json.dumps(result))
        self.assert_unchanged(self.root, before)

    def test_ops_verification_exposes_only_public_evidence_fields(self):
        source = {
            "data_status": "THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY", "checked_at_kst": AT.isoformat(),
            "private": "private content",
            "oracle_inventory": {"database": "private content", "read_only": True, "tables": {
                "ohlcv": {"symbols": 1, "distinct_dates": 2, "trade_date_range": ["2026-11-24", "2026-11-25"],
                          "private": "private content"},
                "financial_snapshots": {"symbols": 1},
                "private": {"private": "private content"},
            }},
            "actual_price_validation": {"results": [{
                "symbol": "005930", "start": "2026-11-24", "end": "2026-11-25", "calculation_dates_checked": 2,
                "source_features_equal_independent_formulas": True, "private": "private content",
            }]},
            "actual_financial_validation": {
                "symbol": "005930", "division": "CFS", "observed_at": AT.isoformat(), "all_equal": True,
                "checks": {"stored_periods_equal": True, "private content": False},
            },
        }
        value = ops.get_ops_overview(self.root, source, now=AT)["verification"]
        self.assertEqual(value["oracle_inventory"]["tables"]["ohlcv"]["distinct_dates"], 2)
        self.assertEqual(value["actual_price_validation"]["results"][0]["symbol"], "005930")
        self.assertEqual(value["actual_financial_validation"]["checks"], {"stored_periods_equal": True})
        self.assertNotIn("private content", json.dumps(value))

    def test_ops_corrupt_sources_are_bounded_and_independent(self):
        self.state.mkdir(parents=True)
        (self.state / "status.json").write_text("{private content", encoding="utf-8")
        (self.state.parent / "news-api-usage.sqlite3").write_bytes(b"private content")
        db = self.state.parent / "stock-assistant.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (id)")
        result = ops.get_ops_overview(self.root, {}, now=AT)
        self.assertEqual(result["holdings"]["reason"], "HOLDINGS_UNAVAILABLE")
        self.assertEqual(result["news_budget"]["reason"], "NEWS_USAGE_STATE_UNAVAILABLE")
        self.assertEqual(result["daytrade"]["reason"], "DAYTRADE_STATUS_UNAVAILABLE")
        self.assertNotIn("private content", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
