import http.client
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / area) for area in ("src", "engine/src", "scripts")]

from oracle_dashboard import OracleDashboardServer
from research_dashboard.server import DashboardServer
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData


class LongTermApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        dist = self.root / "web/dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>Finance</title>", encoding="utf-8")
        env = patch.dict("os.environ", {"STOCK_DATABASE_PATH": "", "STOCK_UNIFIED_RESEARCH_DATABASE": "",
                                       "LONGTERM_LEDGER_PATH": "", "DAYTRADE_STATE_ROOT": ""})
        env.start()
        self.addCleanup(env.stop)

    def start(self, server_type, **kwargs):
        self.server = server_type(self.root, port=0, **kwargs)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def request(self, path, *, method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {}, body=body)
            response = conn.getresponse()
            content = response.read()
            if response.getheader("Content-Type").startswith("application/json"):
                content = json.loads(content)
            return response.status, response.headers, content
        finally:
            conn.close()

    def test_local_session_initialization_and_controls(self):
        self.start(DashboardServer)
        self.assertEqual(self.request("/favicon.ico")[0], 204)
        self.assertEqual(self.request("/api/longterm/account")[0], 401)
        headers = {"Cookie": self.request("/longterm")[1]["Set-Cookie"].split(";")[0]}
        self.assertEqual(self.request("/api/longterm/overview", headers=headers)[2]["status"], "NO_ACCOUNT")
        self.assertFalse(self.server.longterm.path.exists())
        self.assertEqual(self.request("/api/longterm/account/init", method="POST", headers=headers, body="{}")[0], 403)
        headers.update(Origin=f"http://127.0.0.1:{self.server.server_port}", **{"X-Paper-Action": "1"})
        self.assertEqual(self.request("/api/longterm/refresh", method="POST", headers=headers, body="{}")[0], 400)
        self.assertFalse(self.server.longterm.path.exists())
        self.assertEqual(self.request("/api/longterm/account/init", method="POST", headers=headers, body="{}")[0], 503)
        self.assertEqual(self.request("/api/longterm/account", headers=headers)[2]["status"], "NO_ACCOUNT")
        stock = StockRepository(self.root / ".runtime/stock-assistant.sqlite3")
        ResearchData(self.root / ".runtime/unified-research-inputs.sqlite3", source_database=stock.database_path)
        for action, status in (("account/init", "RUNNING"), ("account/pause", "PAUSED"),
                               ("account/resume", "RUNNING"), ("refresh", "RUNNING")):
            response, _, payload = self.request("/api/longterm/" + action, method="POST", headers=headers, body="{}")
            self.assertEqual(response, 200, payload)
            self.assertEqual(payload["status"], status)
        self.assertEqual(self.request("/api/paper-accounts", headers=headers)[0], 200)
        self.assertEqual(self.request("/api/hermes/reports", headers=headers)[0], 200)

    def test_oracle_routes_readonly_sources_and_default_hosts(self):
        self.start(OracleDashboardServer)
        routes = ["/", "/longterm", "/daytrade", "/research", "/swing", "/ops"]
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for route in [*routes, "/hermes"]:
            self.assertEqual(self.request(route)[0], 200)
        self.assertEqual(self.request("/favicon.ico")[0], 204)
        self.assertEqual(self.request("/", headers={"Host": f"localhost:{self.server.server_port}"})[0], 200)
        self.assertEqual(self.request("/", headers={"Host": "finance.example.ts.net"})[0], 403)
        cookie = {"Cookie": self.request("/")[1]["Set-Cookie"].split(";")[0]}
        caps = self.request("/api/capabilities", headers=cookie)[2]
        self.assertEqual(caps["routes"], routes)
        self.assertTrue(caps["phase_b_available"])
        self.assertTrue(caps["read_only"])
        self.assertFalse(caps["orders_enabled"])
        self.assertFalse(caps["telegram_delivery_enabled"])
        self.assertEqual(self.request("/api/longterm/account", headers=cookie)[2]["status"], "NO_ACCOUNT")
        for endpoint in ("research/reports", "swing/reports", "performance", "ops/overview",
                         "daytrade/report", "daytrade/sessions"):
            self.assertEqual(self.request("/api/" + endpoint, headers=cookie)[0], 200)
        for endpoint in ("longterm/account/init", "longterm/account/pause", "longterm/account/resume",
                         "longterm/refresh", "daytrade/simulate-sample", "experiments"):
            self.assertEqual(self.request("/api/" + endpoint, method="POST", headers=cookie, body="{}")[0], 403)
        self.assertEqual(self.request("/api/paper-accounts", headers=cookie)[0], 404)
        self.assertFalse((self.root / ".runtime").exists())

    def test_allowed_host_is_exact_and_preserves_cross_site_guard(self):
        self.start(OracleDashboardServer, allowed_host="Finance.Example.ts.net")
        self.assertEqual(self.request("/hermes", headers={"Host": "finance.example.ts.net"})[0], 200)
        self.assertEqual(self.request("/", headers={"Host": "finance.example.ts.net.attacker.test"})[0], 403)
        self.assertEqual(self.request("/", headers={"Host": "finance.example.ts.net", "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("/", headers={"Host": f"localhost:{self.server.server_port}"})[0], 200)
        with self.assertRaises(ValueError):
            OracleDashboardServer(self.root, port=0, allowed_host="https://finance.example.ts.net/path")
