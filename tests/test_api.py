import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from research_dashboard.server import DashboardServer

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        for name in ("datasets", "artifacts"):
            (root/name).mkdir()
        shutil.copytree(ROOT/"web", root/"web")
        shutil.copyfile(ROOT/"datasets/validation.json",root/"datasets/validation.json")
        shutil.copyfile(ROOT/"artifacts/source-audit.json",root/"artifacts/source-audit.json")
        self.server = DashboardServer(root,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.port=self.server.server_port
        status,headers,_=self.request("GET","/")
        self.cookie=headers["Set-Cookie"].split(";")[0]
        self.assertEqual(status,200)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def request(self,method,path,body=None,authorized=False,extra=None):
        conn=http.client.HTTPConnection("127.0.0.1",self.port,timeout=5)
        headers={}
        if authorized:
            headers.update(Cookie=self.cookie,Origin=f"http://127.0.0.1:{self.port}",**{"X-Paper-Action":"1"})
        headers.update(extra or {})
        conn.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
        response=conn.getresponse()
        status,hs,raw=response.status,dict(response.getheaders()),response.read()
        conn.close()
        data=json.loads(raw) if hs["Content-Type"].startswith("application/json") else raw
        return status,hs,data

    def test_session_csrf_host_and_health(self):
        self.assertEqual(self.request("GET","/api/experiments")[0],401)
        for body in ({}, {"ignored":"x"*64}, {"ignored":"x"*4096}):
            with self.subTest(rejected_body_length=len(json.dumps(body))):
                self.assertEqual(self.request("POST","/api/experiments",body,extra={"Cookie":self.cookie})[0],403)
        self.assertEqual(self.request("POST","/api/experiments",{},True,{"Origin":"https://other.example"})[0],403)
        self.assertEqual(self.request("GET","/",extra={"Host":"evil.example"})[0],403)
        self.assertEqual(self.request("GET","/",extra={"Sec-Fetch-Site":"cross-site"})[0],403)
        health=self.request("GET","/health")[2]
        self.assertFalse(health["orders_enabled"])
        self.assertFalse(health["telegram_delivery_enabled"])

    def test_post_requires_host_matching_origin_non_cross_site_and_an_object_body(self):
        path = "/api/experiments"
        self.assertEqual(self.request("POST", path, {}, True, {"Host": f"localhost:{self.port}"})[0], 403)
        self.assertEqual(self.request("POST", path, {}, True, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("POST", path, authorized=True)[0], 400)
        self.assertEqual(self.request("GET", path, authorized=True)[2], [])

    def test_capabilities_require_a_session_and_declare_phase_b_routes(self):
        path = "/api/capabilities"
        self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("GET", path, authorized=True)[2], {
            "read_only": False,
            "orders_enabled": False,
            "telegram_delivery_enabled": False,
            "routes": ["/", "/longterm", "/daytrade", "/experiments", "/research", "/swing", "/ops"],
            "phase_b_available": True,
            "daytrade_sessions_available": True,
        })

    def test_spa_routes_issue_session_cookie_and_fall_back_to_legacy(self):
        web = self.server.root / "web"
        dist = web / "dist"
        dist.mkdir(exist_ok=True)
        index = dist / "index.html"
        index.write_bytes(b"React fixture")
        for path in ("/?experiment=E-example&event=e&trade=t&report=r", "/hermes?account=P-example",
                     "/daytrade", "/experiments", "/research", "/swing", "/ops"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual((status, body), (200, b"React fixture"))
                self.assertIn("HttpOnly; SameSite=Strict; Path=/", headers["Set-Cookie"])
                self.assertIn("style-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
                self.assertNotIn("Set-Cookie", self.request("GET", path, authorized=True)[1])
        self.assertEqual(self.request("GET", "/research/extra", authorized=True)[0], 404)
        self.assertEqual(self.request("GET", "/api/research/reports")[0], 401)
        index.unlink()
        for path, legacy in (("/", "index.html"), ("/hermes", "hermes.html"), ("/daytrade", "daytrade.html")):
            self.assertEqual(self.request("GET", path)[2], (web / legacy).read_bytes())
        self.assertEqual(self.request("GET", "/experiments", authorized=True)[0], 404)

    def test_spa_assets_use_allowed_content_types_without_creating_sessions(self):
        assets = self.server.root / "web/dist/assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets.parent / "index.html").write_bytes(b"React fixture")
        for filename, mime in (("bundle.js", "text/javascript; charset=utf-8"),
                               ("bundle.css", "text/css; charset=utf-8"), ("font.woff2", "font/woff2"),
                               ("icon.svg", "image/svg+xml"), ("image.png", "image/png")):
            with self.subTest(filename=filename):
                (assets / filename).write_bytes(b"asset fixture")
                status, headers, data = self.request("GET", f"/assets/{filename}?v=1")
                self.assertEqual((status, data), (200, b"asset fixture"))
                self.assertEqual(headers["Content-Type"], mime)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request("GET", "/assets/bundle.js", extra={"Host": "example.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/assets/bundle.css", extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        (assets / "metadata.json").write_bytes(b"{}")
        for path in ("/assets/", "/assets/missing.js", "/assets/metadata.json", "/assets/sub/../bundle.js",
                     "/assets/%2E%2E%2Fbundle.js", "/assets/%2e%2e%5cbundle.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_spa_rejects_symbolic_links_in_assets_and_index(self):
        web = self.server.root / "web"
        dist = web / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        target = assets / "fixture-original.js"
        target.write_bytes(b"asset fixture")
        outside = web / "fixture-outside.js"
        outside.write_bytes(b"outside fixture")
        try:
            (assets / "fixture-alias.js").symlink_to(target)
            (assets / "fixture-outside.js").symlink_to(outside)
        except OSError as error:
            self.skipTest(f"Creating symlinks is unavailable: {error}")
        index = dist / "index.html"
        index.write_bytes(b"React fixture")
        current = self.server.root / "current-release"
        current.symlink_to(self.server.root, target_is_directory=True)
        self.server.root = current
        self.assertEqual(self.request("GET", "/")[2], b"React fixture")
        self.assertEqual(self.request("GET", "/assets/fixture-original.js")[2], b"asset fixture")
        for name in ("fixture-alias.js", "fixture-outside.js"):
            self.assertEqual(self.request("GET", f"/assets/{name}")[0], 404)
        index.unlink()
        index.symlink_to(web / "hermes.html")
        self.assertEqual(self.request("GET", "/")[2], (web / "index.html").read_bytes())
        self.assertEqual(self.request("GET", "/assets/fixture-original.js")[0], 404)
        index.unlink()
        index.write_bytes(b"React fixture")
        original_assets = dist / "fixture-original-assets"
        assets.rename(original_assets)
        assets.symlink_to(original_assets, target_is_directory=True)
        self.assertEqual(self.request("GET", "/assets/fixture-original.js")[0], 404)

    def test_create_start_pause_resume_and_get_is_read_only(self):
        status,_,created=self.request("POST","/api/experiments",{"cost_bps":"30"},True)
        self.assertEqual(status,201)
        path="/api/experiments/"+created["experiment_id"]
        self.assertEqual(self.request("POST",path+"/start",{},True)[0],200)
        before=self.request("GET",path,authorized=True)[2]
        self.assertEqual(self.request("GET",path,authorized=True)[2],before)
        self.assertEqual(self.request("POST",path+"/pause",{},True)[2]["state"]["status"],"PAUSED")
        self.assertEqual(self.request("POST",path+"/resume",{},True)[2]["state"]["status"],"RUNNING")
        self.server.store.apply(created["experiment_id"],limit=100)
        completed=self.request("GET",path,authorized=True)[2]
        self.assertEqual(completed["state"]["status"],"COMPLETED")
        self.assertEqual(self.request("POST",path+"/start",{},True)[0],400)
        notices=self.request("GET",path+"/drafts",authorized=True)[2]
        self.assertTrue(all(n["delivery"]=="DISABLED_PENDING_APPROVAL" for n in notices))

    def test_malformed_unknown_and_unconfigured_real_mode(self):
        self.assertEqual(self.request("POST","/api/experiments",{"cost_bps":"NaN"},True)[0],400)
        self.assertEqual(self.request("POST","/api/experiments",{"cash_policy":"SHRINK"},True)[0],400)
        self.assertEqual(self.request("GET","/api/experiments/E-"+"0"*24,authorized=True)[0],404)

    def test_status_metadata_preserves_actual_verification_limits(self):
        initial=self.request("GET","/api/status",authorized=True)[2]
        self.assertFalse(initial['production_db_accessed'])
        self.assertIsNone(initial['actual_financial_validation'])
        # Keep local operational inventories outside Git; use explicit fixture metadata.
        inventory={'checked_at_kst':'2026-01-01T00:00:00+09:00','fixture':True}
        (self.server.root/'artifacts/oracle-data-coverage.json').write_text(
            json.dumps(inventory),encoding='utf-8')
        for name in ('observed-price-calculation.json','observed-financial-calculation.json'):
            shutil.copyfile(ROOT/'artifacts'/name,self.server.root/'artifacts'/name)
        status=self.request("GET","/api/status",authorized=True)[2]
        self.assertTrue(status['production_db_accessed'])
        self.assertEqual(status['oracle_inventory'],inventory)
        self.assertFalse(status['production_db_changed'])
        self.assertEqual(status['data_status'],'THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY')
        self.assertFalse(status['actual_price_validation']['historical_decisions_certified'])
        self.assertEqual(status['actual_price_validation']['calculation_dates_total'],125)
        self.assertTrue(status['actual_financial_validation']['all_equal'])
        self.assertFalse(status['actual_financial_validation']['historical_point_in_time_certified'])

    def test_hermes_account_api_reads_and_controls_are_separate(self):
        aid=self.server.accounts.create_account()
        path='/api/paper-accounts/'+aid
        self.assertEqual(self.request('GET',path)[0],401)
        result=self.request('GET',path,authorized=True)[2]
        self.assertEqual(self.request('GET',path,authorized=True)[2]['state'],result['state'])
        self.assertEqual(self.request('GET','/api/paper-accounts',authorized=True)[2]['accounts'],[aid])
        self.assertEqual(self.request('POST',path+'/pause',{},extra={'Cookie':self.cookie})[0],403)
        self.assertEqual(self.request('POST',path+'/pause',{},True)[2]['state']['status'],'PAUSED')
        self.assertEqual(self.request('POST',path+'/resume',{},True)[2]['state']['status'],'RUNNING')
        self.assertEqual(self.request('POST',path+'/resume',{'cost_bps':'10'},True)[0],400)
        self.assertEqual(self.request('GET','/api/experiments',authorized=True)[2],[])
        self.assertEqual(self.request('GET','/hermes')[0],200)

