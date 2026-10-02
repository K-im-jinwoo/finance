import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'vendor/stock-assistant/src'))
from shared_toss_auth import SharedTokenCache,SharedTossMarketDataClient
from stock_assistant.providers.http import AuthenticationError

def concurrent_process(directory):
    store=SharedTokenCache(directory,'SYNTHETIC-CLIENT')
    def issue():
        counter=Path(directory)/'synthetic-issue-count'
        value=int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(value+1))
        time.sleep(0.05)
        return {'access_token':'SYNTHETIC-TOKEN','expires_in':86400}
    assert store.get(issue)=='SYNTHETIC-TOKEN'

class SharedAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name).resolve()/'auth-cache'
    def tearDown(self):self.temp.cleanup()
    def issue(self):return {'access_token':'SYNTHETIC-TOKEN','expires_in':86400}

    def test_distinct_clients_reuse_same_token_with_one_issue(self):
        issue=Mock(side_effect=self.issue)
        first=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT')
        second=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT')
        self.assertEqual(first.get(issue),second.get(issue))
        issue.assert_called_once()

    def test_simultaneous_threads_issue_once(self):
        issue=Mock(side_effect=self.issue)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:SharedTokenCache(self.directory,'SYNTHETIC-CLIENT').get(issue),range(16)))
        self.assertEqual(set(results),{'SYNTHETIC-TOKEN'})
        issue.assert_called_once()

    def test_separate_processes_issue_once(self):
        context=multiprocessing.get_context('spawn')
        processes=[context.Process(target=concurrent_process,args=(str(self.directory),)) for _ in range(4)]
        for process in processes:process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():process.terminate();process.join(3)
            self.assertEqual(process.exitcode,0)
        self.assertEqual((self.directory/'synthetic-issue-count').read_text(),'1')

    def test_expiry_renews_under_same_lock(self):
        clock=[100.0]
        store=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT',clock=lambda:clock[0])
        issue=Mock(return_value={'access_token':'SYNTHETIC-TOKEN','expires_in':10})
        store.get(issue);clock[0]=108;store.get(issue)
        self.assertEqual(issue.call_count,1)
        clock[0]=109;store.get(issue)
        self.assertEqual(issue.call_count,2)

    def test_late_failure_cannot_expire_newer_token(self):
        store=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT')
        first=store.get(self.issue);store.invalidate(first)
        second=store.get(lambda:{'access_token':'SYNTHETIC-NEW','expires_in':86400})
        store.invalidate(first)
        issue=Mock()
        self.assertEqual(store.get(issue),second)
        issue.assert_not_called()

    def test_different_client_and_corrupt_cache_fail_without_issuing(self):
        store=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT');store.get(self.issue)
        issue=Mock()
        with self.assertRaisesRegex(ValueError,'SHARED_AUTH_CACHE_CLIENT_CHANGED'):
            SharedTokenCache(self.directory,'DIFFERENT-CLIENT').get(issue)
        store.path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError,'SHARED_AUTH_CACHE_INVALID'):store.get(issue)
        issue.assert_not_called()

    def test_invalid_upstream_responses_do_not_create_token_file(self):
        store=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT')
        for payload in [None,{}, {'access_token':'token','expires_in':True}, {'access_token':'token','expires_in':0}]:
            with self.subTest(payload=payload),self.assertRaisesRegex(ValueError,'INVALID_SHARED_AUTH_RESPONSE'):
                store.get(lambda:payload)
            self.assertFalse(store.path.exists())

    def test_posix_cache_permissions_are_private(self):
        if os.name=='nt':self.skipTest('POSIX mode check requires Linux')
        store=SharedTokenCache(self.directory,'SYNTHETIC-CLIENT');store.get(self.issue)
        self.assertEqual(self.directory.stat().st_mode&0o777,0o700)
        self.assertEqual(store.path.stat().st_mode&0o777,0o600)
        store.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'AUTH_CACHE_FILE_PERMISSIONS_REQUIRED'):store.get(self.issue)

    def test_provider_preserves_public_quotes_and_handles_rejection(self):
        observed=datetime.now(timezone.utc)
        payload={'result':[{'symbol':'240810','lastPrice':'100','currency':'KRW','timestamp':observed.isoformat()}]}
        fetch=Mock(return_value=Mock(payload=payload))
        issue=Mock(return_value=Mock(payload=self.issue()))
        first=SharedTossMarketDataClient('SYNTHETIC-CLIENT','SYNTHETIC-SECRET',cache_dir=self.directory,fetch_json=fetch,post_form=issue)
        second=SharedTossMarketDataClient('SYNTHETIC-CLIENT','SYNTHETIC-SECRET',cache_dir=self.directory,fetch_json=fetch,post_form=issue)
        a=first.prices(['240810'],observed_at=observed);b=second.prices(['240810'],observed_at=observed)
        self.assertEqual(a[0].last_price,b[0].last_price)
        issue.assert_called_once()
        fetch.side_effect=AuthenticationError('synthetic rejection')
        with self.assertRaises(AuthenticationError):first.prices(['240810'],observed_at=observed)
        fetch.side_effect=None
        second.prices(['240810'],observed_at=observed)
        self.assertEqual(issue.call_count,2)
        self.assertTrue(all(call.args[0].endswith('/api/v1/prices') for call in fetch.call_args_list))
