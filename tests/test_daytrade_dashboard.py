"""Loopback API and isolated sample fixtures; no Oracle/WS/Telegram calls."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from research_dashboard import daytrade_bridge as bridge
from research_dashboard.server import DashboardServer

FIXTURE = ROOT / "engine/tests/fixtures/daytrade_bot_sample.json"


class PageSafety(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "style" or "style" in attributes or any(name.startswith("on") for name in attributes):
            raise AssertionError("inline style or event handler")
        if tag == "script" and not attributes.get("src", "").startswith("/"):
            raise AssertionError("external self script required")
        if tag == "a":
            self.links.add(attributes.get("href"))


class DaytradeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def sample(self, relative="datasets/daytrade_bot_sample.json"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, path)
        return path

    def test_absent_does_not_create_any_files_or_use_foreign_checkout_fixture(self):
        report = bridge.read_daytrade_report(self.root)
        self.assertEqual(report, {"present":False,"orders_enabled":False,"paper_ready":False,
                                 "sample_available":False,"report":None,"markdown":None})
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(FileNotFoundError): bridge.simulate_sample_daytrade(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_both_explicit_fixture_locations_and_dataset_priority(self):
        self.sample("engine/tests/fixtures/daytrade_bot_sample.json")
        self.assertTrue(bridge.read_daytrade_report(self.root)["sample_available"])
        preferred = self.sample()
        preferred.write_text('{"dataset_kind":"OBSERVATION"}', encoding="utf-8")
        with self.assertRaises(ValueError): bridge.simulate_sample_daytrade(self.root)
        self.assertFalse((self.root / ".runtime/intraday-paper.sqlite3").exists())

    def test_simulate_readonly_snapshot_and_no_overwrite(self):
        self.sample()
        result = bridge.simulate_sample_daytrade(self.root)
        path = self.root / ".runtime/intraday-paper.sqlite3"
        before = path.read_bytes()
        self.assertTrue(result["present"])
        self.assertEqual(result["report"]["account"]["completed_trades"], 2)
        self.assertEqual(result, bridge.read_daytrade_report(self.root))
        with self.assertRaises(FileExistsError): bridge.simulate_sample_daytrade(self.root)
        self.assertEqual(before, path.read_bytes())
        ledger = bridge._ReadOnlyLedger(self.root)
        try:
            self.assertEqual(ledger.connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                ledger.connection.execute("DELETE FROM meta")
            self.assertTrue(ledger.audit()["cash_identity"])
        finally:
            ledger.close()
        self.assertEqual(before, path.read_bytes())

    def test_read_only_uri_and_connection_closed_on_renderer_failure(self):
        self.sample()
        bridge.simulate_sample_daytrade(self.root)
        opened, closed = [], []
        connect = sqlite3.connect
        close = bridge._ReadOnlyLedger.close

        def capture(*args, **kw):
            opened.append((args, kw))
            return connect(*args, **kw)

        def close_capture(ledger):
            closed.append(ledger.connection)
            close(ledger)

        with patch.object(bridge.sqlite3, "connect", capture), \
                patch.object(bridge._ReadOnlyLedger, "close", close_capture), \
                patch.object(bridge, "render_markdown_report", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError): bridge.read_daytrade_report(self.root)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][0][0].endswith("?mode=ro"))
        self.assertTrue(opened[0][1]["uri"])
        self.assertEqual(len(closed), 1)
        with self.assertRaises(sqlite3.ProgrammingError): closed[0].execute("SELECT 1")

    def test_src_only_pythonpath_from_foreign_cwd_loads_canonical_engine(self):
        code = "import json,stock_assistant; print(json.dumps(stock_assistant.__file__))"
        # Import the bridge first: no bootstrap and no engine/src in PYTHONPATH.
        code = "import research_dashboard.daytrade_bridge; " + code
        result = subprocess.run([sys.executable,"-S","-B","-c",code], cwd=self.root,
            env=os.environ | {"PYTHONPATH":str(ROOT / "src")}, capture_output=True, text=True,
            encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(json.loads(result.stdout)).resolve().is_relative_to((ROOT / "engine/src").resolve()))


class DaytradeDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "web", self.root / "web")
        self.server = DashboardServer(self.root, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        status, headers, _ = self.request("GET", "/daytrade")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def sample(self):
        path = self.root / "datasets/daytrade_bot_sample.json"
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(FIXTURE, path)
        return path

    def request(self, method, path, body=None, *, authorized=False, headers=None, raw=None):
        request_headers = {"Cookie":self.cookie,"Origin":f"http://127.0.0.1:{self.port}",
                           "X-Paper-Action":"1"} if authorized else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=raw if raw is not None else json.dumps(body) if body is not None else None,
                           headers=request_headers)
        response = connection.getresponse()
        status, response_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, json.loads(data) if response_headers["Content-Type"].startswith("application/json") else data

    def test_static_routes_cookie_csp_three_way_navigation_and_script_syntax(self):
        for path in ("/daytrade", "/daytrade.js"):
            status, headers, data = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn("HttpOnly; SameSite=Strict", headers["Set-Cookie"])
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        for name in ("index.html", "hermes.html", "daytrade.html"):
            parser = PageSafety()
            parser.feed((ROOT / "web" / name).read_text(encoding="utf-8"))
            self.assertTrue({"/", "/hermes", "/daytrade"}.issubset(parser.links))
        source = (ROOT / "web/daytrade.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("outerHTML", source)
        result = subprocess.run(["node", "--check", str(ROOT / "web/daytrade.js")],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_authentication_csrf_origin_action_and_local_host_guards(self):
        self.assertEqual(self.request("GET", "/api/daytrade/report")[0], 401)
        for headers in ({}, {"Cookie":self.cookie},
                        {"Cookie":self.cookie,"Origin":f"http://127.0.0.1:{self.port}"},
                        {"Cookie":self.cookie,"X-Paper-Action":"1"},
                        {"Cookie":self.cookie,"X-Paper-Action":"1","Origin":"https://other.example"}):
            self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", {}, headers=headers)[0], 403)
        self.assertEqual(self.request("GET", "/api/daytrade/report", authorized=True, headers={"Host":"evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/daytrade/report", authorized=True, headers={"Sec-Fetch-Site":"cross-site"})[0], 403)
        self.assertFalse((self.root / ".runtime/intraday-paper.sqlite3").exists())

    def test_missing_sample_404_and_no_implicit_fallback(self):
        report = self.request("GET", "/api/daytrade/report", authorized=True)[2]
        self.assertFalse(report["present"])
        self.assertFalse(report["sample_available"])
        self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", {}, authorized=True)[0], 404)
        self.assertFalse((self.root / ".runtime/intraday-paper.sqlite3").exists())

    def test_end_to_end_sample_201_read_report_and_repeat_409_preserves_all_ledgers(self):
        self.sample()
        protected = [self.root / ".runtime" / name for name in ("paper.sqlite3", "hermes-paper.sqlite3")]
        before = {p:sha256(p.read_bytes()).hexdigest() for p in protected}
        absent = self.request("GET", "/api/daytrade/report", authorized=True)[2]
        self.assertFalse(absent["present"])
        self.assertTrue(absent["sample_available"])
        status, _, result = self.request("POST", "/api/daytrade/simulate-sample", {}, authorized=True)
        self.assertEqual(status, 201)
        self.assertTrue(result["present"])
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["paper_ready"])
        report = result["report"]
        self.assertEqual(report["account"]["completed_trades"], 2)
        self.assertEqual(report["account"]["net_pnl"], "91159.2040")
        self.assertEqual(len(report["fills"]), 4)
        self.assertIn("당일 단타 오프라인 모의거래 리포트", result["markdown"])
        path = self.root / ".runtime/intraday-paper.sqlite3"
        raw = path.read_bytes()
        self.assertEqual(self.request("GET", "/api/daytrade/report", authorized=True)[2], result)
        self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", {}, authorized=True)[0], 409)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(before, {p:sha256(p.read_bytes()).hexdigest() for p in protected})
        self.assertEqual(self.request("GET", "/api/experiments", authorized=True)[2], [])
        self.assertEqual(self.request("GET", "/api/paper-accounts", authorized=True)[2]["accounts"], [])

    def test_post_requires_explicit_empty_object_and_bounded_body(self):
        self.sample()
        for raw in ('[]', 'null', '{"cost_bps":0}', '{', ''):
            self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", authorized=True, raw=raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", authorized=True, raw=' ' * 8193)[0], 413)
        self.assertFalse((self.root / ".runtime/intraday-paper.sqlite3").exists())

    def test_invalid_sample_and_corrupt_ledger_fail_without_leaking_or_overwrite(self):
        self.sample().write_text('{"dataset_kind":"OBSERVATION"}', encoding="utf-8")
        self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", {}, authorized=True)[0], 400)
        path = self.root / ".runtime/intraday-paper.sqlite3"
        self.assertFalse(path.exists())
        path.write_bytes(b"corrupt-fixture")
        status, _, result = self.request("GET", "/api/daytrade/report", authorized=True)
        self.assertEqual(status, 503)
        self.assertEqual(result, {"error":"DAYTRADE_REPORT_UNAVAILABLE"})
        self.assertEqual(self.request("POST", "/api/daytrade/simulate-sample", {}, authorized=True)[0], 409)
        self.assertEqual(path.read_bytes(), b"corrupt-fixture")

    def test_concurrent_sample_posts_create_one_ledger(self):
        self.sample()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(self.request, "POST", "/api/daytrade/simulate-sample", {}, authorized=True)
                     for _ in range(2)]
            statuses = sorted(task.result()[0] for task in tasks)
        self.assertEqual(statuses, [201, 409])
        report = self.request("GET", "/api/daytrade/report", authorized=True)[2]["report"]
        self.assertEqual(report["account"]["completed_trades"], 2)


    def test_stream_sample_preference_and_persistent_provisional_cost_notice(self):
        self.sample()
        stream=self.root/'datasets/daytrade_observed_stream_sample.json'
        shutil.copyfile(ROOT/'engine/tests/fixtures/daytrade_observed_stream_sample.json',stream)
        status,_,result=self.request('POST','/api/daytrade/simulate-sample',{},authorized=True)
        self.assertEqual(status,201)
        report=result['report']
        self.assertEqual(report.get('cost_policy_status'),'PROVISIONAL_DEV_UNAPPROVED')
        self.assertEqual(report['fixture_costs']['buy_fee_bps'],'2.0')
        self.assertEqual(report['fixture_costs']['sell_fee_bps'],'2.0')
        self.assertEqual(report['fixture_costs']['sell_tax_bps'],'20')
        self.assertIn('개발용 잠정',report['cost_policy_notice'])
        self.assertIn('공식',result['markdown'])
        self.assertEqual(self.request('GET','/api/daytrade/report',authorized=True)[2],result)

    def test_api_reads_approved_session_progress_without_writing(self):
        from stock_assistant.daytrade.stream import LivePaperSession
        source=json.loads((ROOT/'engine/tests/fixtures/daytrade_observed_stream_sample.json').read_text(encoding='utf-8'))
        with LivePaperSession(self.root,websocket_slot_available=True) as session:
            session.add(source['events'][0])
            path=self.root/'.runtime/intraday-paper.sqlite3'
            before=path.read_bytes()
            status,_,result=self.request('GET','/api/daytrade/report',authorized=True)
            self.assertEqual(status,200)
            report=result['report']
            self.assertTrue(report['paper_ready'])
            self.assertFalse(report['orders_enabled'])
            self.assertEqual(report['cost_policy_status'],'APPROVED_CONSERVATIVE_V1')
            self.assertEqual(report['sessions'],{'2026-11-13':'OBSERVING'})
            self.assertIn('PAPER_READY=true',result['markdown'])
            self.assertEqual(before,path.read_bytes())


if __name__ == "__main__":
    unittest.main()
