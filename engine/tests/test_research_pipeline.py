from contextlib import closing, redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from stock_assistant.financial_research import evaluate_financial
from stock_assistant.financial_research_state import FinancialResearchState
from stock_assistant.models import Decision
from stock_assistant.reports import build_candidate_report, report_to_dict
from stock_assistant.repository import StockRepository
from stock_assistant.research_cli import main, review_universe
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.screening import screen_security
from stock_assistant.screening_diagnostics import audit_screening
from tests.test_screening import AS_OF, good_financial, samsung
from tests.helpers import make_bars
from tests.test_financial_research import history


class ResearchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.sqlite3"
        self.repo = StockRepository(self.path)
        self.repo.save_securities([samsung()])
        self.repo.save_financial_snapshot(good_financial())

    def test_audit_has_minimum_fields_and_cannot_change_source_database(self):
        candidate = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=good_financial(),
                                    financing_data_available=False)
        report = report_to_dict(build_candidate_report(AS_OF, [candidate], news_discovery={"status": "OK", "candidates": [
            {"symbol": "005930", "status": "REVIEW_REQUIRED"}, {"symbol": "000660", "status": "EXCLUDED"}]}))
        self.repo.save_report(report["report_id"], report["as_of"], report)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        readonly = ReadOnlyResearchRepository(self.path)
        result = audit_screening(readonly, as_of=AS_OF)
        self.assertEqual(result["decision_counts"], {"BUY_HOLD": 1})
        self.assertEqual(result["warning_counts"]["DILUTION_HISTORY_UNAVAILABLE"], 1)
        self.assertEqual(result["news_fixed_hold_label_observations"], 1)
        self.assertEqual(result["data_coverage"]["financial_fields"]["turnover_history_present"], 1)
        self.assertNotIn("checks", result["latest_candidates"][0])
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        with self.assertRaises(sqlite3.OperationalError):
            readonly.save_financial_snapshot(good_financial())

    def test_empty_database_is_not_migrated_by_audit(self):
        empty = Path(self.directory.name) / "empty.sqlite3"
        sqlite3.connect(empty).close()
        result = audit_screening(ReadOnlyResearchRepository(empty), as_of=AS_OF)
        self.assertEqual(result["reason"], "REPORT_TABLE_MISSING")
        with closing(sqlite3.connect(empty)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_universe_includes_missing_financials_without_price_shortlist(self):
        other = replace(samsung(), symbol="000660", name="다른 기업")
        self.repo.save_securities([other])
        reviews = review_universe(ReadOnlyResearchRepository(self.path), as_of=AS_OF)
        self.assertEqual({review.symbol: review.gate for review in reviews}, {"000660": "VERIFY_PENDING", "005930": "PASS"})

    def test_state_reuses_identical_evaluation_and_closes_completed_verification(self):
        path = Path(self.directory.name) / "research.sqlite3"
        state = FinancialResearchState(path, source_database=self.path)
        review = evaluate_financial(samsung(), good_financial(), as_of=AS_OF)
        self.assertEqual(state.sync([review], as_of=AS_OF)["changed_evaluations"], 1)
        later = AS_OF + timedelta(hours=1)
        same = evaluate_financial(samsung(), good_financial(), as_of=later)
        self.assertEqual(state.sync([same], as_of=later)["reused_evaluations"], 1)
        complete = evaluate_financial(samsung(), good_financial(), as_of=later, annual_history=history())
        result = state.sync([complete], as_of=later)
        self.assertEqual(result["changed_evaluations"], 1)
        self.assertEqual(result["verification_task_counts"], {"RESOLVED": 1})
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 2)

    def test_state_cannot_write_source_or_reverse_time_or_accept_duplicate_symbols(self):
        with self.assertRaises(ValueError):
            FinancialResearchState(self.path, source_database=self.path)
        state = FinancialResearchState(Path(self.directory.name) / "state.sqlite3", source_database=self.path)
        review = evaluate_financial(samsung(), good_financial(), as_of=AS_OF)
        state.sync([review], as_of=AS_OF)
        with self.assertRaises(ValueError):
            state.sync([review, review], as_of=AS_OF)
        earlier = AS_OF - timedelta(hours=1)
        with self.assertRaises(ValueError):
            state.sync([evaluate_financial(samsung(), good_financial(), as_of=earlier)], as_of=earlier)

    def test_unrelated_existing_database_is_rejected_without_schema_changes(self):
        other = Path(self.directory.name) / "other.sqlite3"
        with closing(sqlite3.connect(other)) as connection:
            connection.execute("CREATE TABLE user_data(value TEXT)")
            connection.commit()
        before = other.read_bytes()
        with self.assertRaises(ValueError):
            FinancialResearchState(other, source_database=self.path)
        self.assertEqual(before, other.read_bytes())

    def test_inapplicable_tasks_are_not_claimed_as_completed_and_pending_age_is_preserved(self):
        path = Path(self.directory.name) / "state.sqlite3"
        state = FinancialResearchState(path, source_database=self.path)
        review = evaluate_financial(samsung(), good_financial(), as_of=AS_OF)
        state.sync([review], as_of=AS_OF)
        with closing(sqlite3.connect(path)) as connection:
            opened_at = connection.execute("SELECT opened_at FROM verification_tasks").fetchone()[0]
            record = json.loads(connection.execute("SELECT payload_json FROM evaluations").fetchone()[0])
            self.assertEqual(record["source_inputs"]["financial"]["symbol"], "005930")
            self.assertGreater(record["recorded_at"], AS_OF.isoformat())
        later = AS_OF + timedelta(hours=1)
        state.sync([evaluate_financial(samsung(), good_financial(), as_of=later)], as_of=later)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT opened_at FROM verification_tasks").fetchone()[0], opened_at)
        excluded = evaluate_financial(samsung(), replace(good_financial(), operating_cash_flow=Decimal("0")), as_of=later)
        self.assertEqual(state.sync([excluded], as_of=later)["verification_task_counts"], {"NOT_REQUIRED": 1})

    def test_annual_history_cli_rejects_non_numeric_input_without_traceback(self):
        path = Path(self.directory.name) / "bad-history.json"
        item = {"symbol": "005930", "period_end": "2025-12-31", "operating_income": True,
                "published_at": "2026-03-01T00:00:00+00:00", "observed_at": "2026-03-02T00:00:00+00:00",
                "receipt_id": "20260000000001", "statement_basis": "CFS", "source_url": "https://dart.fss.or.kr/example"}
        path.write_text(json.dumps([item]), encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["financials", "--database", str(self.path), "--annual-history", str(path)]), 1)
        self.assertIn("must be numeric", errors.getvalue())

    def test_read_only_cli_explicit_state_is_isolated_and_not_a_paper_signal(self):
        output = io.StringIO()
        before = self.path.read_bytes()
        with redirect_stdout(output):
            result = main(["financials", "--database", str(self.path), "--as-of", AS_OF.isoformat(),
                           "--state-database", str(Path(self.directory.name) / "state.sqlite3")])
        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["gate_counts"], {"PASS": 1})
        self.assertFalse(payload["paper_account_eligible"])
        self.assertEqual(payload["next_news_review_symbols"], ["005930"])
        self.assertEqual(before, self.path.read_bytes())

    def test_cli_invalid_source_or_asof_or_same_state_does_not_create_source(self):
        missing = Path(self.directory.name) / "missing.sqlite3"
        for args in (["audit", "--database", str(missing)],
                     ["audit", "--database", str(self.path), "--reports", "21"],
                     ["financials", "--database", str(self.path), "--state-database", str(self.path)],
                     ["financials", "--database", str(self.path), "--as-of", "2026-09-20T12:00:00"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 1)
        self.assertFalse(missing.exists())

    def test_future_report_is_not_included(self):
        candidate = screen_security(samsung(), make_bars(), as_of=AS_OF, financial=good_financial())
        report = report_to_dict(build_candidate_report(AS_OF + timedelta(days=1), [candidate]))
        self.repo.save_report(report["report_id"], report["as_of"], report)
        result = audit_screening(ReadOnlyResearchRepository(self.path), as_of=AS_OF)
        self.assertEqual(result["status"], "NO_REPORTS")

