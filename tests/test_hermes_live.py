import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, Mock
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from research_dashboard.hermes_reports import parse_report
from research_dashboard.live_accounts import LiveAccounts, quote_view
from research_dashboard.contracts import number
from research_dashboard.oracle_bridge import HermesLiveWorker

T0 = datetime(2026,9,30,4,0,tzinfo=timezone.utc)
OPEN = datetime(2026,10,1,0,0,tzinfo=timezone.utc)

def report(symbol='111111', published=T0, suffix='A'):
    content=f'''## CIO 최종판정
- **보고서 ID:** `R-20260930T0400Z-AAAAAAA{suffix}`
### 우선 검토 후보
**1. 첫 후보 ({symbol})**
- **검토 등급:** 우선 검토 후보
- **행동 판정:** 매수 보류
- **핵심 이유:** 검증용 사유
- **추가 확인 조건:** 공시 확인
- **무효화 조건:** 종가 평균 비교
**2. 두 번째 후보 (222222)**
- **검토 등급:** 우선 검토 후보
- **행동 판정:** 매수 보류
- **핵심 이유:** 별도 사유
'''
    return {'message_id':1,'timestamp':published.timestamp(),'finish_reason':'stop','content':content}

def parsed(symbol='111111',published=T0,suffix='A',observed=T0):
    return parse_report(report(symbol,published,suffix),observed.isoformat())

def opening(price='10000', now=OPEN+timedelta(seconds=90)):
    return {'open_at':OPEN.isoformat(),'bar_end':(OPEN+timedelta(minutes=1)).isoformat(),
            'session_id':'KR-REGULAR-2026-10-01','basis':'UNADJUSTED','prices':{'111111':price},
            'volumes':{'111111':10},'observed_at':now.isoformat(),'source':'PUBLIC_TEST_FEED'}

def quote(price='11000',at=OPEN+timedelta(seconds=90)):
    return {'symbol':'111111','last_price':price,'source_timestamp':at.isoformat(),'observed_at':at.isoformat(),'currency':'KRW','source':'PUBLIC_TEST_FEED'}

class HermesReportTests(unittest.TestCase):
    def test_literal_rank_selection_original_hold_and_full_text(self):
        message=report()
        result=parse_report(message,T0.isoformat())
        self.assertEqual(result['selected']['symbol'],'111111')
        self.assertEqual(result['selected']['decision'],'BUY_HOLD')
        self.assertEqual(result['selected']['conditions'],'공시 확인')
        self.assertEqual(result['content'],message['content'])
        self.assertEqual(result['content_sha256'],hashlib.sha256(message['content'].encode()).hexdigest())
        self.assertEqual(len(result['candidates']),2)

    def test_ambiguous_unranked_incomplete_and_unknown_fail_closed(self):
        for old,new in [('**1.','**'),('**2.','**1.'),('222222','111111'),('행동 판정:','다른 판정:'),('매수 보류','판정 미확인')]:
            with self.subTest(change=new):
                row=report();row['content']=row['content'].replace(old,new)
                self.assertIsNone(parse_report(row,T0.isoformat())['selected'])
        row=report();row['finish_reason']='length'
        self.assertIsNone(parse_report(row,T0.isoformat())['selected'])

    def test_numbered_heading_order_does_not_replace_explicit_rank(self):
        row=report();text=row['content'];first=text.index('**1.');second=text.index('**2.')
        row['content']=text[:first]+text[second:]+text[first:second]
        self.assertEqual(parse_report(row,T0.isoformat())['selected']['symbol'],'111111')

    def test_future_or_changed_hash_rejected(self):
        with self.assertRaises(ValueError):parse_report(report(),(T0-timedelta(seconds=1)).isoformat())
        row=report();row['content_sha256']='0'*64
        with self.assertRaises(ValueError):parse_report(row,T0.isoformat())

class LiveAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=LiveAccounts(Path(self.temp.name)/'paper.sqlite3')
        self.aid=self.store.create_account(T0.isoformat())
        self.store.worker_started(T0.isoformat())
        self.store.update(self.aid,[parsed()],now=T0.isoformat(),calendar={'regular_open':OPEN.isoformat(),'observed_at':T0.isoformat(),'source':'OFFICIAL_TEST_CALENDAR'})

    def tearDown(self):self.temp.cleanup()

    def test_only_top_one_pending_no_past_fill_and_old_results_preserved(self):
        state=self.store.get_account(self.aid,T0.isoformat())['state']
        self.assertEqual(set(state['pending']),{'111111'})
        self.assertEqual(state['trades'],[])
        self.assertEqual(state['positions'],{})
        self.assertEqual(len(self.store.reports()[0]['candidates']),2)
        second=self.store.create_account((T0+timedelta(seconds=1)).isoformat())
        self.assertNotEqual(self.aid,second)
        self.assertEqual(self.store.get_account(self.aid,T0.isoformat())['state'],state)

    def test_fill_budget_integer_cash_reconciled_and_cost_return(self):
        now=OPEN+timedelta(seconds=90)
        result=self.store.update(self.aid,quotes=[quote()],opened=opening(),now=now.isoformat())
        p=result['state']['positions']['111111'];q=result['position_quotes']['111111']
        self.assertEqual(p['quantity'],199)
        self.assertEqual(p['entry_fee'],'2985')
        self.assertEqual(result['state']['cash'],'8007015')
        self.assertEqual(q['pnl'],'196015')
        self.assertEqual(number(q['return']),number('2189000')/number('1992985')-1)
        self.assertEqual(result['current_assets'],'10196015')
        self.assertEqual(result['state']['trades'][0]['signal_at'],T0.isoformat())
        self.assertEqual(result['state']['trades'][0]['filled_at'],OPEN.isoformat())
        again=self.store.update(self.aid,[parsed()],quotes=[quote()],opened=opening(),now=(now+timedelta(seconds=1)).isoformat())
        self.assertEqual(len(again['state']['trades']),1)

    def test_missing_stale_future_and_pre_entry_quotes_never_current_pnl(self):
        now=OPEN+timedelta(seconds=90)
        self.store.update(self.aid,quotes=[quote()],opened=opening(),now=now.isoformat())
        stale=self.store.get_account(self.aid,(now+timedelta(seconds=301)).isoformat())
        self.assertIsNone(stale['position_quotes']['111111']['return'])
        self.assertIsNone(stale['current_assets'])
        p=stale['state']['positions']['111111']
        self.assertIsNone(quote_view(None,now.isoformat(),p)['return'])
        self.assertIsNone(quote_view(quote(at=now+timedelta(seconds=1)),now.isoformat(),p)['return'])
        self.assertIsNone(quote_view(quote(at=OPEN-timedelta(seconds=1)),now.isoformat(),p)['return'])
        self.assertEqual(quote_view(quote('0'),now.isoformat(),p)['freshness'],'INVALID')

    def test_pause_cancels_resume_requires_newly_published_cio_report(self):
        paused=self.store.control(self.aid,'pause',(T0+timedelta(seconds=1)).isoformat())
        self.assertFalse(paused['state']['pending'])
        during=parsed('333333',T0+timedelta(seconds=2),'B',T0+timedelta(seconds=2))
        self.store.update(self.aid,[during],now=(T0+timedelta(seconds=2)).isoformat())
        self.store.control(self.aid,'resume',(T0+timedelta(seconds=3)).isoformat())
        result=self.store.update(self.aid,[during],now=(T0+timedelta(seconds=4)).isoformat())
        self.assertFalse(result['state']['pending'])
        fresh=parsed('444444',T0+timedelta(seconds=5),'C',T0+timedelta(seconds=5))
        result=self.store.update(self.aid,[fresh],now=(T0+timedelta(seconds=5)).isoformat())
        self.assertEqual(set(result['state']['pending']),{'444444'})

    def test_restart_and_late_incomplete_adjusted_zero_open_rejected(self):
        cases=[(opening(),OPEN+timedelta(seconds=30)),(opening(),OPEN+timedelta(seconds=301))]
        for key,value in [('basis','ADJUSTED'),('bar_end',OPEN.isoformat()),('volumes',{'111111':0})]:
            event=opening();event[key]=value;cases.append((event,OPEN+timedelta(seconds=90)))
        for event,now in cases:
            with self.subTest(event=event,now=now):
                with self.assertRaises(ValueError):self.store.update(self.aid,opened=event,now=now.isoformat())
                self.assertFalse(self.store.get_account(self.aid,now.isoformat())['state']['trades'])
        self.store.worker_started((OPEN+timedelta(seconds=1)).isoformat())
        with self.assertRaises(ValueError):self.store.update(self.aid,opened=opening(),now=(OPEN+timedelta(seconds=90)).isoformat())

    def test_unknown_evidence_is_not_an_invalid_or_valid_signal(self):
        checks=self.store.get_account(self.aid,T0.isoformat())['state']['checks']['111111']
        self.assertEqual([c['status'] for c in checks],['UNKNOWN','UNKNOWN','REVIEW_REQUIRED'])
        self.assertEqual(self.store.get_account(self.aid,T0.isoformat())['state']['invalidated'],{})

    def test_report_fingerprint_dedup_survives_restart(self):
        restarted=LiveAccounts(self.store.path)
        result=restarted.update(self.aid,[parsed()],now=(T0+timedelta(seconds=1)).isoformat())
        self.assertEqual(len(result['state']['reports']),1)
        self.assertEqual(len(restarted.reports()),1)
        with restarted.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM paper_events WHERE event_id=?",(parsed()['key'],)).fetchone()[0],1)

    def test_direct_credentials_do_not_enable_unapproved_open_policy(self):
        now=OPEN+timedelta(seconds=90)
        reader=Mock()
        reader.request.side_effect=[{'messages':[report()],'retrieved_at':now.isoformat()},
            {'quotes':[quote()],'feed_mode':'DEDICATED_PUBLIC_REST','source_update_seconds':30,
             'calendar':{'regular_open':OPEN.isoformat(),'observed_at':T0.isoformat(),'source':'OFFICIAL_TEST_CALENDAR'},'opened':opening()}]
        with patch('research_dashboard.oracle_bridge.SSHReader',return_value=reader),patch('research_dashboard.oracle_bridge.utc_now',return_value=now.isoformat()):
            worker=HermesLiveWorker(ROOT,self.store,threading.Event(),direct=True)
            worker.poll()
        result=self.store.get_account(self.aid,now.isoformat())
        self.assertEqual(result['state']['trades'],[])
        self.assertEqual(set(result['state']['pending']),{'111111'})
        self.assertEqual(result['meta']['feed']['execution_gate'],'OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED')

