"""Raw public stream integration; fee expectations are independent literal cash flows."""
from copy import deepcopy
from decimal import Decimal
import importlib
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from stock_assistant.daytrade.ledger import DaytradeLedger
from stock_assistant.daytrade.http import HttpResult, RestAdapter
from stock_assistant.daytrade.websocket import WebSocketAdapter
from daytrade_harness import FakeServer

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'engine/tests/fixtures/daytrade_observed_stream_sample.json'


def sample():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class StreamTests(unittest.TestCase):
    def stream(self):
        self.assertIsNotNone(importlib.util.find_spec('stock_assistant.daytrade.stream'), 'public stream integration missing')
        return importlib.import_module('stock_assistant.daytrade.stream')

    def test_stream_to_fill_costs_and_reopened_dashboard_notice(self):
        stream = self.stream()
        with tempfile.TemporaryDirectory() as folder:
            report = stream.run_observed_stream(sample(), folder)
            self.assertEqual(report['account']['completed_trades'], 2)
            self.assertEqual(Decimal(report['account']['net_pnl']), Decimal('93581.2040'))
            self.assertEqual([f['fee'] for f in report['fills']], ['398', '398', '420', '397'])
            self.assertEqual([f['tax'] for f in report['fills']], ['0', '0', '4198', '3974'])
            self.assertEqual(report['cost_policy_status'], 'PROVISIONAL_DEV_UNAPPROVED')
            self.assertIn('2.0', report['cost_policy_notice'])
            self.assertIn('공식', report['cost_policy_notice'])
            self.assertFalse(report['orders_enabled']); self.assertFalse(report['paper_ready'])
            ledger = DaytradeLedger(folder)
            try: self.assertTrue(ledger.audit()['cash_identity'])
            finally: ledger.close()
            sys.path.insert(0, str(ROOT / 'src'))
            from research_dashboard.daytrade_bridge import read_daytrade_report
            reopened = read_daytrade_report(folder)
            self.assertEqual(reopened['report'], report)
            self.assertIn('PROVISIONAL_DEV_UNAPPROVED', reopened['markdown'])
            self.assertIn('93581.2040', reopened['markdown'])

    def test_capture_bridge_real_adapter_callbacks_replay_same_results(self):
        stream = self.stream()
        receive = {}
        bridge = stream.LiveSessionBridge(initial_capital='10000000', seed=42, stream_kind='SYNTHETIC',
            receive_clock=lambda: receive['at'], monotonic_clock=lambda: receive['mono'])
        for row in sample()['events']:
            if row['kind'] == 'ws_frame':
                server = FakeServer()
                receive.update(at=row['at'],mono=row['received_monotonic'])
                adapter = WebSocketAdapter(server.connect, rng=random.Random(1), on_message=bridge.on_frame)
                adapter.replace({row['frame']['topic']}, now=0)
                adapter.connect(now=0); adapter.poll(now=0)
                server.sockets[-1].push(row['frame']); adapter.poll(now=1)
                server.sockets[-1].close()
            elif row['kind'] in {'rankings','candles'}:
                route = '/api/v1/rankings' if row['kind']=='rankings' else '/api/v1/candles'
                adapter = RestAdapter(lambda *args, r=row: HttpResult(200, r['payload'], {}), rng=random.Random(2))
                response = adapter.fetch(route, {}, now=10)
                bridge.on_rest(route, response, at=row['at'], symbol=row.get('symbol'),
                    ranking_type=row.get('ranking_type'), risk_meta=row.get('risk_meta'), received_monotonic=row.get('received_monotonic',0))
            else: bridge.add(row)
        self.assertFalse(bridge.orders_enabled); self.assertFalse(bridge.paper_ready)
        with tempfile.TemporaryDirectory() as folder:
            report = bridge.run(folder)
            self.assertEqual(Decimal(report['account']['net_pnl']), Decimal('93581.2040'))
            self.assertEqual(report['sessions'], {'2026-11-13':'CLOSED'})

    def test_private_topics_currency_and_source_time_errors_prevent_ledger_creation(self):
        stream = self.stream()
        for mutation in ('topic','currency','future','order','risk_meta','ranking_price'):
            data = sample()
            row = next(e for e in data['events'] if e['kind']=='ws_frame')
            if mutation=='topic': row['frame']['topic']='account:kr:005930'
            elif mutation=='currency': row['frame']['data']['currency']='USD'
            elif mutation=='future': row['frame']['data']['timestamp']='2099-01-01T00:00:00Z'
            elif mutation=='order': data['events'][0]['kind']='order'
            else:
                ranked=next(e for e in data['events'] if e['kind']=='rankings' and e['ranking_type']=='MARKET_TRADING_AMOUNT')
                if mutation=='risk_meta': ranked['risk_meta']={'005930':{'halted':'false'}}
                else: ranked['payload']['result']['rankings'][0]['price']['lastPrice']='invalid'
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ValueError): stream.run_observed_stream(data, folder)
                self.assertFalse((Path(folder)/'.runtime/intraday-paper.sqlite3').exists())

    def test_stale_rankings_and_transport_degraded_block_new_entries(self):
        stream = self.stream()
        for variant in ('stale','rate_limit','globally_blocked'):
            data=sample()
            ranking=next(e for e in data['events'] if e['kind']=='rankings')
            if variant=='stale': ranking['payload']['result']['rankedAt']='2026-11-13T10:03:00+09:00'
            else:
                data['events'].insert(1, {'kind':'runtime_status','id':'degraded','at':data['events'][0]['at'],
                    'data':{'group':'RANKINGS','status':'RATE_LIMIT_DEGRADED' if variant=='rate_limit' else 'OK','entry_blocked':True}})
                for i,e in enumerate(data['events']): e['sequence']=i
            with tempfile.TemporaryDirectory() as folder:
                report=stream.run_observed_stream(data,folder)
                self.assertFalse(report['fills'])
                if variant=='rate_limit': self.assertTrue(any('RATE_LIMIT_DEGRADED' in r['locks'] for r in report['risk_events']))

    def test_later_volume_snapshot_retains_known_risk_metadata(self):
        stream=self.stream(); data=sample()
        index=next(i for i,e in enumerate(data['events']) if e['kind']=='rankings' and e['ranking_type']=='MARKET_TRADING_AMOUNT')
        later=deepcopy(data['events'][index]);later['id']='later-volume';later['ranking_type']='MARKET_TRADING_VOLUME'
        later.pop('risk_meta',None);data['events'].insert(index+1,later)
        for i,e in enumerate(data['events']):e['sequence']=i
        with tempfile.TemporaryDirectory() as folder:
            report=stream.run_observed_stream(data,folder)
            rejected={row['symbol']:row['reason'] for row in report['scan_history'][-1]['rejected']}
            self.assertEqual(rejected.get('035420'),'HALTED')

    def test_overrides_remain_unapproved_and_duplicate_frames_do_not_double_fill(self):
        stream = self.stream()
        data=sample();data['events'].append(deepcopy(data['events'][1]))
        data['fixture_costs']={'buy_fee_bps':'3.0','version':'CUSTOM-DEV'}
        with tempfile.TemporaryDirectory() as folder:
            report=stream.run_observed_stream(data,folder)
            self.assertEqual(len(report['fills']),4)
            self.assertEqual(report['fixture_costs']['buy_fee_bps'],'3.0')
            self.assertEqual(report['cost_policy_status'],'PROVISIONAL_DEV_UNAPPROVED')
            self.assertIn('3.0',report['cost_policy_notice'])

    def test_run_stream_cli_outputs_are_reopened_without_policy_loss(self):
        self.stream()
        environment=os.environ | {'PYTHONPATH':str(ROOT/'engine/src'),'PYTHONDONTWRITEBYTECODE':'1'}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'session';md=Path(folder)/'report.md';js=Path(folder)/'report.json'
            command=[sys.executable,'-m','stock_assistant.daytrade','run-stream',str(FIXTURE),'--root',str(root),'--report-md',str(md),'--report-json',str(js)]
            result=subprocess.run(command,capture_output=True,encoding='utf-8',env=environment,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            report=json.loads(js.read_text(encoding='utf-8'))
            self.assertEqual(report['cost_policy_status'],'PROVISIONAL_DEV_UNAPPROVED')
            self.assertEqual(report['account']['completed_trades'],2)
            self.assertIn('공식',md.read_text(encoding='utf-8'))
            repeated=subprocess.run(command,capture_output=True,encoding='utf-8',env=environment,timeout=30)
            self.assertEqual(repeated.returncode,1)


class PaperReadyTests(unittest.TestCase):
    def stream(self):
        stream=importlib.import_module('stock_assistant.daytrade.stream')
        self.assertTrue(hasattr(stream,'LivePaperSession'), 'incremental paper session missing')
        return stream

    def read_report(self, root):
        sys.path.insert(0,str(ROOT/'src'))
        from research_dashboard.daytrade_bridge import read_daytrade_report
        return read_daytrade_report(root)['report']

    def test_approved_batch_costs_persist_but_provisional_cannot_claim_ready(self):
        from dataclasses import asdict
        stream=self.stream()
        with tempfile.TemporaryDirectory() as folder:
            report=stream.run_paper_ready_stream(sample(),folder)
            self.assertTrue(report['paper_ready'])
            self.assertEqual(report['cost_policy_status'],'APPROVED_CONSERVATIVE_V1')
            self.assertEqual(report['fixture_costs']['version'],'STAGE5-APPROVED-CONSERVATIVE-V1')
            self.assertEqual([f['fee'] for f in report['fills']],['398','398','420','397'])
            self.assertEqual(Decimal(report['account']['net_pnl']),Decimal('93581.2040'))
            self.assertFalse(report['orders_enabled'])
            self.assertEqual(self.read_report(folder),report)
        from stock_assistant.daytrade.bot import run_paper_session
        from stock_assistant.daytrade.report import render_markdown_report
        forged=stream.stream_to_dataset(sample())
        forged.update(cost_policy_status='APPROVED_CONSERVATIVE_V1',paper_ready=True)
        with tempfile.TemporaryDirectory() as folder:
            report=run_paper_session(forged,folder)
            self.assertFalse(report['paper_ready'])
            self.assertNotEqual(report['cost_policy_status'],'APPROVED_CONSERVATIVE_V1')
            self.assertIn('PAPER_READY=false',render_markdown_report(report))

    def test_unapproved_overrides_and_unconfirmed_slot_never_create_ledger(self):
        stream=self.stream()
        for costs in ({'buy_fee_bps':'3.0'},{'sell_tax_bps':'0'},{'latency_ms':0}):
            with tempfile.TemporaryDirectory() as folder:
                data=sample(); data['fixture_costs']=costs
                with self.assertRaises(ValueError): stream.run_paper_ready_stream(data,folder)
                self.assertFalse((Path(folder)/'.runtime/intraday-paper.sqlite3').exists())
        for flag in (False,None,1):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ValueError): stream.LivePaperSession(folder,websocket_slot_available=flag)
                self.assertFalse((Path(folder)/'.runtime/intraday-paper.sqlite3').exists())
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError): stream.LivePaperSession(folder,websocket_slot_available=True,fixture_costs={'buy_fee_bps':'3'})
            self.assertFalse((Path(folder)/'.runtime/intraday-paper.sqlite3').exists())
            data=sample();data['orders_enabled']=True
            with self.assertRaises(ValueError): stream.run_paper_ready_stream(data,folder)
            self.assertFalse((Path(folder)/'.runtime/intraday-paper.sqlite3').exists())

    def test_incremental_callbacks_publish_progress_and_refresh_subscriptions(self):
        stream=self.stream()
        adapter=WebSocketAdapter(FakeServer().connect,rng=random.Random(1))
        with tempfile.TemporaryDirectory() as folder:
            session=stream.LivePaperSession(folder,websocket_slot_available=True,ws_adapter=adapter,stream_kind='SYNTHETIC')
            try:
                self.assertTrue(self.read_report(folder)['paper_ready'])
                for row in sample()['events']:
                    kind=row['kind'];stamp={'at':row['at'],'received_monotonic':row['received_monotonic']}
                    if kind=='session': session.on_session(normal_session_verified=True,**stamp)
                    elif kind=='ws_frame': session.on_frame(row['frame'],**stamp)
                    elif kind in {'rankings','candles'}:
                        path='/api/v1/rankings' if kind=='rankings' else '/api/v1/candles'
                        session.on_rest(path,{'status':'OK','payload':row['payload']},symbol=row.get('symbol'),
                            ranking_type=row.get('ranking_type'),risk_meta=row.get('risk_meta'),**stamp)
                    elif kind=='clock': session.on_clock(**stamp)
                    else: session.close_session(**stamp)
                    progress=self.read_report(folder)
                    self.assertTrue(progress['paper_ready']);self.assertFalse(progress['orders_enabled'])
                    if kind=='rankings' and row['ranking_type']=='MARKET_TRADING_AMOUNT':
                        self.assertEqual(adapter.desired,{'trade:kr:005930','orderbook:kr:005930','trade:kr:000660','orderbook:kr:000660'})
                        self.assertEqual(len(progress['scan_history']),1)
                    if row['id']=='buy-profit':
                        self.assertEqual(len(progress['fills']),1)
                        self.assertEqual(progress['positions']['005930']['quantity'],181)
                        self.assertEqual(progress['sessions'],{'2026-11-13':'OBSERVING'})
                        ranked=deepcopy(next(e for e in sample()['events'] if e['kind']=='rankings' and e['ranking_type']=='MARKET_TRADING_AMOUNT'))
                        ranked['risk_meta']['005930']={'halted':True}
                        session.on_rest('/api/v1/rankings',{'status':'OK','payload':ranked['payload']},
                            ranking_type='MARKET_TRADING_AMOUNT',risk_meta=ranked['risk_meta'],**stamp)
                        self.assertIn('orderbook:kr:005930',adapter.desired)
                self.assertEqual(Decimal(progress['account']['net_pnl']),Decimal('93581.2040'))
                self.assertEqual(progress['account']['completed_trades'],2)
                self.assertTrue(progress['audit']['cash_identity'])
                self.assertEqual(session.report(),progress)
                self.assertEqual(progress['sessions'],{'2026-11-13':'CLOSED'})
                path=Path(folder)/'.runtime/intraday-paper.sqlite3';before=path.read_bytes()
                with self.assertRaises(FileExistsError):stream.LivePaperSession(folder,websocket_slot_available=True)
                self.assertEqual(before,path.read_bytes())
            finally: session.close()

    def test_degraded_response_locks_now_and_duplicate_callback_changes_nothing(self):
        stream=self.stream()
        with tempfile.TemporaryDirectory() as folder:
            with stream.LivePaperSession(folder,websocket_slot_available=True) as session:
                first=sample()['events'][0]
                session.add(first)
                path=Path(folder)/'.runtime/intraday-paper.sqlite3';before=path.read_bytes()
                self.assertFalse(session.add(first));self.assertEqual(path.read_bytes(),before)
                session.on_rest('/api/v1/rankings',{'status':'RATE_LIMIT_DEGRADED','entry_blocked':True},at='2026-11-13T09:00:01+09:00')
                report=self.read_report(folder)
                self.assertIn('RATE_LIMIT_DEGRADED',report['risk_events'][-1]['locks'])
                self.assertFalse(report['fills'])
                self.assertTrue(report['paper_ready'])

    def test_paper_ready_cli_and_markdown_reopening(self):
        self.stream()
        environment=os.environ|{'PYTHONPATH':str(ROOT/'engine/src'),'PYTHONDONTWRITEBYTECODE':'1'}
        with tempfile.TemporaryDirectory() as folder:
            md=Path(folder)/'report.md';root=Path(folder)/'session'
            result=subprocess.run([sys.executable,'-m','stock_assistant.daytrade','run-paper-ready',str(FIXTURE),'--root',str(root),'--report-md',str(md)],capture_output=True,encoding='utf-8',env=environment,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            report=json.loads(result.stdout)
            self.assertTrue(report['paper_ready']);self.assertFalse(report['orders_enabled'])
            self.assertIn('PAPER_READY=true',md.read_text(encoding='utf-8'))
            self.assertIn('승인된 보수적 표준안',md.read_text(encoding='utf-8'))
            self.assertEqual(self.read_report(root),report)

    def test_live_export_keeps_approval_without_second_execution(self):
        stream=self.stream()
        with tempfile.TemporaryDirectory() as folder:
            with stream.LivePaperSession(folder,websocket_slot_available=True) as session:
                session.add(sample()['events'][0])
                exported=session.to_dataset()
                self.assertEqual(exported['cost_policy_status'],'APPROVED_CONSERVATIVE_V1')
                self.assertTrue(exported['paper_ready'])
                other=Path(folder)/'second-ledger'
                with self.assertRaises(ValueError):session.run(other)
                self.assertFalse(other.exists())


if __name__=='__main__': unittest.main()
