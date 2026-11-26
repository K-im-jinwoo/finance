"""Offline HTTP contract for new read-only Phase B endpoints."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from research_dashboard.server import DashboardServer


class PhaseBApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.root.joinpath("web").mkdir()
        cls.root.joinpath("web/index.html").write_text("<!doctype html>", encoding="utf-8")
        cls.environment = patch.dict("os.environ", {
            "STOCK_DATABASE_PATH": str(cls.root / "absent-stock.db"),
            "STOCK_UNIFIED_RESEARCH_DATABASE": str(cls.root / "absent-research.db"),
            "STOCK_NEWS_USAGE_FILE": str(cls.root / "absent-news.db"),
            "DAYTRADE_STATE_ROOT": str(cls.root / "absent-state"),
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.server = DashboardServer(cls.root, 0)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.addClassCleanup(cls.shutdown)
        status, headers, _ = cls.request("/")
        assert status == 200
        cls.cookie = headers["Set-Cookie"].split(";", 1)[0]

    @classmethod
    def shutdown(cls):
        cls.server.shutdown()
        cls.worker.join(5)
        cls.server.server_close()

    @classmethod
    def request(cls, path, *, cookie=None, extra=None, method="GET", body=None):
        headers = {"Cookie": cookie} if cookie else {}
        headers.update(extra or {})
        connection = http.client.HTTPConnection("127.0.0.1", cls.server.server_port, timeout=10)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            payload = response.read()
            if response.headers["Content-Type"].startswith("application/json"):
                payload = json.loads(payload)
            return response.status, dict(response.headers), payload
        finally:
            connection.close()

    def test_phase_b_endpoints_require_session_and_preserve_guards(self):
        for path in ("/api/research/reports", "/api/research/reports/UR-0123456789abcdef01234567",
                     "/api/research/chart/005930?report_id=UR-0123456789abcdef01234567",
                     "/api/swing/reports", "/api/swing/reports/SW-0123456789abcdef01234567",
                     "/api/performance", "/api/ops/overview", "/api/daytrade/sessions",
                     "/api/daytrade/report?date=2026-11-25"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
                self.assertEqual(self.request(path, cookie=self.cookie, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
                status, headers, payload = self.request(path, cookie=self.cookie)
                self.assertEqual(status, 200, payload)
                self.assertIs(payload["present"], False)
                self.assertTrue(payload["reason"])
                self.assertIn("style-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])

    def test_phase_b_capabilities_and_invalid_query_do_not_create_state(self):
        caps = self.request("/api/capabilities", cookie=self.cookie)[2]
        self.assertIs(caps["phase_b_available"], True)
        self.assertIs(caps["daytrade_sessions_available"], True)
        self.assertIs(caps["orders_enabled"], False)
        for path in (
            "/api/research/reports/UR-%2e%2e%2fsecret",
            "/api/research/chart/005930?report_id=UR-1&report_id=UR-2",
            "/api/performance?report_id=../../private",
            "/api/daytrade/report?date=../../private",
            "/api/daytrade/report?date=2026-02-30",
        ):
            with self.subTest(path=path):
                status, _, payload = self.request(path, cookie=self.cookie)
                self.assertIn(status, {400, 404}, payload)
        self.assertFalse(self.root.joinpath("absent-stock.db").exists())
        self.assertFalse(self.root.joinpath("absent-research.db").exists())
        self.assertFalse(self.root.joinpath("absent-news.db").exists())
        self.assertFalse(self.root.joinpath("absent-state").exists())


    def test_corrupt_oracle_artifact_is_reported_without_breaking_ops(self):
        with tempfile.TemporaryDirectory(dir=self.root) as artifact_dir:
            scratch = Path(artifact_dir)
            scratch.joinpath("artifacts").mkdir()
            scratch.joinpath("artifacts/source-audit.json").write_text("{}", encoding="utf-8")
            scratch.joinpath("artifacts/oracle-data-coverage.json").write_text("[]", encoding="utf-8")
            with patch.object(self.server, "root", scratch):
                status, _, payload = self.request("/api/ops/overview", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(payload["verification"], {"present": False, "reason": "VERIFICATION_UNAVAILABLE"})
