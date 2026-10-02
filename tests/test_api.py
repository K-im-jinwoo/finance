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
        for name in ("web", "datasets", "artifacts"):
            (root/name).mkdir()
        for path in (ROOT/"web").iterdir():
            shutil.copyfile(path,root/"web"/path.name)
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
        for name in ('oracle-data-coverage.json','observed-price-calculation.json','observed-financial-calculation.json'):
            shutil.copyfile(ROOT/'artifacts'/name,self.server.root/'artifacts'/name)
        status=self.request("GET","/api/status",authorized=True)[2]
        self.assertTrue(status['production_db_accessed'])
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

