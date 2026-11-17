"""Offline notification contracts; senders are injected and never use Telegram."""
from decimal import Decimal
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from stock_assistant.daytrade.ledger import DaytradeLedger
from stock_assistant.daytrade.stream import run_paper_ready_stream

FIXTURE=Path(__file__).parent/'fixtures/daytrade_observed_stream_sample.json'


class TelegramFillTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('stock_assistant.daytrade.telegram'), 'fill notifier missing')
        return importlib.import_module('stock_assistant.daytrade.telegram')

    def ledger(self):
        root=tempfile.TemporaryDirectory();self.addCleanup(root.cleanup)
        report=run_paper_ready_stream(json.loads(FIXTURE.read_text(encoding='utf-8')),root.name)
        ledger=DaytradeLedger(root.name);self.addCleanup(ledger.close)
        with ledger.connection:
            ledger.connection.execute('INSERT INTO meta VALUES(?,?)',('symbol_names',json.dumps({'005930':'삼성전자','000660':'SK하이닉스'})))
        return ledger,report

    def test_buy_message_uses_korean_name_units_precision_and_fill_time(self):
        text=self.module().format_fill_telegram_message({'event_id':'fill-buy','symbol':'114800','side':'BUY',
            'at':'2026-11-16T02:42:04+00:00','quantity':1534,'gross':'1999201.7604','fee':'400','tax':'0',
            'signal':{'score':100,'reasons':['VWAP_BREAKOUT','RISING_CLOSE','VOLUME_SPIKE','BID_DEPTH']}},
            symbol_names={'114800':'KODEX 인버스'},account={'cash':'8000398.2396','realized_pnl_net':'0'},positions={'114800':1534})
        for expected in ('🟢 [단타 모의체결 · 매수]','KODEX 인버스 (114800)','2026-11-16 11:42:04 KST',
            '1,534주','평균 체결가: 1,303원','199만 9,202원','수수료 400원','분봉 근사 VWAP 돌파',
            '조건 충족률 100%','800만 398원','보유 1종목','PAPER ONLY','실주문 없음'):
            self.assertIn(expected,text)
        self.assertNotIn('.2606',text)

    def test_sell_message_includes_net_realized_costs_and_cumulative_profit(self):
        text=self.module().format_fill_telegram_message({'event_id':'fill-sell','symbol':'114800','side':'SELL',
            'at':'2026-11-16T02:55:42+00:00','quantity':1534,'gross':'2007604.3988','fee':'402','tax':'4015',
            'realized_pnl':'3585.6384','signal':{'exit_reason':'MOMENTUM_WEAK'}},
            symbol_names={'114800':'KODEX 인버스'},account={'cash':'10003585.6384','realized_pnl_net':'3585.6384'},positions={})
        for expected in ('🔵 [단타 모의체결 · 매도]','평균 체결가: 1,309원','수수료 402원 · 세금 4,015원',
                         '실현 순손익: +3,586원','청산 사유: 모멘텀 약화','1,000만 3,586원','누적 실현손익: +3,586원','보유 0종목'):
            self.assertIn(expected,text)
        self.assertNotIn('.6384',text)

    def test_success_is_persistent_and_duplicate_runs_never_send_again(self):
        module=self.module();ledger,report=self.ledger();sent=[]
        def send(text):sent.append(text);return {'status':'SENT','http_status':200}
        results=module.notify_new_fills(ledger,send)
        self.assertEqual(len(sent),4);self.assertEqual([r['status'] for r in results],['SENT']*4)
        self.assertEqual(module.notify_new_fills(ledger,send),[])
        other=DaytradeLedger(ledger.path.parent.parent)
        try:
            ids=json.loads(other.connection.execute("SELECT value FROM meta WHERE key='telegram_notified_fill_ids'").fetchone()[0])
            self.assertEqual(set(ids),{f['event_id'] for f in report['fills']})
            self.assertEqual(module.notify_new_fills(other,send),[])
        finally:other.close()
        self.assertEqual(len(sent),4)

    def test_failure_never_leaks_exception_text_or_changes_financial_history(self):
        module=self.module();ledger,_=self.ledger();before=ledger.snapshot();calls=[]
        def fail(text):calls.append(text);raise TimeoutError('sensitive-transport-value')
        results=module.notify_new_fills(ledger,fail)
        self.assertEqual([r['status'] for r in results],['FAILED']*4)
        self.assertEqual(module.notify_new_fills(ledger,fail),[])
        self.assertEqual(len(calls),4)
        after=ledger.snapshot()
        for key in ('state','events','fills'):self.assertEqual(after[key],before[key])
        metadata=list(ledger.connection.execute("SELECT value FROM meta WHERE key LIKE 'telegram_%'"))
        self.assertNotIn('sensitive-transport-value',json.dumps([results,metadata]))
        self.assertTrue(ledger.audit()['cash_identity'])

    def test_historical_messages_use_state_after_each_fill_not_final_account(self):
        module=self.module();ledger,_=self.ledger();sent=[]
        module.notify_new_fills(ledger,lambda text:(sent.append(text) or {'status':'SENT','http_status':200}))
        self.assertIn('보유 1종목',sent[0]);self.assertIn('보유 2종목',sent[1]);self.assertIn('보유 0종목',sent[-1])
        self.assertNotIn('체결 후 현금: 1,009만',sent[0])
        self.assertIn('종목: 삼성전자 (005930)',sent[0]);self.assertIn('종목: SK하이닉스 (000660)',sent[1])

    def test_sender_does_not_hold_sqlite_write_lock_during_network_wait(self):
        module=self.module();ledger,_=self.ledger()
        def send(text):
            other=sqlite3.connect(ledger.path,timeout=.05)
            try:
                with other:other.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('independent-writer','true'))
            finally:other.close()
            return {'status':'SENT','http_status':200}
        self.assertEqual([r['status'] for r in module.notify_new_fills(ledger,send)],['SENT']*4)

    def test_crash_after_reservation_cannot_trigger_duplicate_on_restart(self):
        module=self.module();ledger,_=self.ledger()
        def crash(text):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):module.notify_new_fills(ledger,crash)
        calls=[]
        results=module.notify_new_fills(ledger,lambda text:(calls.append(text) or {'status':'SENT','http_status':200}))
        self.assertEqual(len(calls),3);self.assertEqual(len(results),3)
        self.assertEqual(module.notify_new_fills(ledger,crash),[])

    def test_api_rejection_is_safe_failed_state_with_no_automatic_retry(self):
        module=self.module();ledger,_=self.ledger()
        result=module.notify_new_fills(ledger,lambda text:{'status':'FAILED','http_status':429,'description':'sensitive-transport-value'})
        self.assertTrue(all(r['status']=='FAILED' and r['http_status']==429 for r in result))
        self.assertNotIn('sensitive-transport-value',json.dumps(result))
        self.assertEqual(module.notify_new_fills(ledger,lambda text:self.fail('must not retry an attempted send')),[])


if __name__=='__main__':unittest.main()
