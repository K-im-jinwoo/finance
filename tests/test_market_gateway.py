import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
sys.path.insert(0,str(ROOT/'engine/src'))
import market_gateway as gateway
import register_oracle_market_auth as registration
from stock_assistant.providers.http import AuthenticationError


class PublicGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.secrets=Path(self.temp.name)
        self.auth_patch=patch.object(gateway,'AUTH_DIR',self.secrets)
        self.auth_patch.start()
        self.server=gateway.PublicDataServer(('127.0.0.1',0))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.auth_patch.stop()
        self.temp.cleanup()

    def credentials(self,shared=False):
        for name,value in {'dashboard-toss-client-id':'OPERATING' if shared else 'SEPARATE',
                           'dashboard-toss-client-secret':'PRIVATE-TEST-SECRET',
                           'toss-client-id':'OPERATING'}.items():
            (self.secrets/name).write_text(value)

    def request(self,method='POST',path='/public-market',payload=None,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            body=json.dumps(payload if payload is not None else {'operation':'public_market','mode':'DIRECT','symbols':['240810']})
            connection.request(method,path,body=body if method=='POST' else None,
                               headers={'Host':'127.0.0.1:9130',**(headers or {})})
            response=connection.getresponse()
            return response.status,json.loads(response.read())
        finally:connection.close()

    def test_missing_auth_is_visible_and_does_not_issue_token(self):
        with patch.object(gateway.market,'direct') as direct:
            status,result=self.request('GET','/health')
            self.assertEqual((status,result['status']),(200,'AUTH_NOT_REGISTERED'))
            self.assertFalse(result['orders_enabled'])
            status,result=self.request()
            self.assertEqual((status,result['error']),(503,'AUTH_NOT_REGISTERED'))
            direct.assert_not_called()

    def test_same_client_is_rejected_before_public_call(self):
        self.credentials(shared=True)
        with patch.object(gateway.market,'direct') as direct:
            status,result=self.request()
            self.assertEqual((status,result['error']),(503,'SHARED_OPERATING_CLIENT_FORBIDDEN'))
            direct.assert_not_called()

    def test_separate_public_quote_marks_success_without_returning_credentials(self):
        self.credentials()
        with patch.object(gateway.market,'direct',return_value={'quotes':[{'symbol':'240810','last_price':'100'}]}) as direct:
            status,result=self.request()
            self.assertEqual(status,200)
            self.assertEqual(result['quotes'][0]['symbol'],'240810')
            self.assertNotIn('PRIVATE-TEST-SECRET',json.dumps(result))
            direct.assert_called_once_with(['240810'])
            _,health=self.request('GET','/health')
            self.assertEqual(health['status'],'AUTH_VERIFIED')
            self.assertTrue(health['last_success_at'])

    def test_order_store_and_extra_fields_are_rejected(self):
        self.credentials()
        with patch.object(gateway.market,'direct') as direct:
            for payload in [{'operation':'order','symbols':['240810']},
                            {'operation':'public_market','mode':'STORE','symbols':['240810']},
                            {'operation':'public_market','symbols':['240810'],'account':'PRIVATE'}]:
                with self.subTest(payload=payload):self.assertEqual(self.request(payload=payload)[0],400)
            for payload in ({}, {'ignored':'x'*64}, {'ignored':'x'*4096}):
                with self.subTest(rejected_path_body=len(json.dumps(payload))):
                    self.assertEqual(self.request(path='/orders',payload=payload)[0],404)
            direct.assert_not_called()

    def test_symbols_are_bounded_and_ambiguous_symbols_are_rejected(self):
        self.credentials()
        with patch.object(gateway.market,'direct') as direct:
            for symbols in [[],['240810','240810'],['../secret'],[240810],['240810']*7]:
                with self.subTest(symbols=symbols):
                    self.assertEqual(self.request(payload={'operation':'public_market','symbols':symbols})[0],400)
            direct.assert_not_called()

    def test_browser_origin_and_public_host_are_rejected(self):
        self.credentials()
        with patch.object(gateway.market,'direct') as direct:
            for payload in ({}, {'ignored':'x'*64}, {'ignored':'x'*4096}):
                with self.subTest(rejected_origin_body=len(json.dumps(payload))):
                    self.assertEqual(self.request(headers={'Origin':'http://127.0.0.1:8765'},payload=payload)[0],403)
                    self.assertEqual(self.request(headers={'Host':'external.example:9130'},payload=payload)[0],403)
            direct.assert_not_called()

    def test_upstream_errors_are_sanitized_and_network_error_keeps_token(self):
        self.credentials()
        client=Mock();client._access_token='PRIVATE-TOKEN'
        error=RuntimeError('PRIVATE-TEST-SECRET')
        error.__cause__=HTTPError('https://public.example',429,'PRIVATE',{},None)
        with patch.object(gateway.market,'client',client),patch.object(gateway.market,'direct',side_effect=error):
            status,result=self.request()
            self.assertEqual(status,502)
            self.assertEqual(result['upstream_http_status'],429)
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertEqual(client._access_token,'PRIVATE-TOKEN')

    def test_authentication_error_clears_only_separate_cached_token(self):
        self.credentials()
        client=Mock();client._access_token='PRIVATE-TOKEN'
        with patch.object(gateway.market,'client',client),patch.object(gateway.market,'direct',side_effect=AuthenticationError('PRIVATE')):
            status,result=self.request()
            self.assertEqual(status,502)
            self.assertIsNone(client._access_token)
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_supplied_token_expiry_is_reported_without_issuance_or_file_mutation(self):
        path=self.secrets/'dashboard-toss-access-token'
        path.write_text('PRIVATE-TOKEN')
        client=gateway.market.SuppliedAccessTokenClient(path)
        client._token()
        with patch.object(gateway.market,'client',client),patch.object(gateway.market,'direct',side_effect=AuthenticationError('PRIVATE')) as direct,patch('stock_assistant.providers.toss.post_form_json') as issue:
            status,result=self.request()
            self.assertEqual((status,result['error']),(502,'SUPPLIED_ACCESS_TOKEN_REJECTED_OR_EXPIRED'))
            self.assertEqual(path.read_text(),'PRIVATE-TOKEN')
            self.assertNotIn('PRIVATE',json.dumps(result))
            direct.assert_called_once();issue.assert_not_called()
            with self.assertRaises(AuthenticationError):client._token()
            path.write_text('REPLACEMENT-TOKEN')
            self.assertEqual(client._token(),'REPLACEMENT-TOKEN')
            _,health=self.request('GET','/health')
            self.assertEqual(health['status'],'PUBLIC_DATA_ERROR')


class CredentialRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()/'dashboard'
        self.root.mkdir();(self.root/'secrets').mkdir()
        self.operating=Path(self.temp.name)/'operating-id'
        self.operating.write_text('OPERATING')
        self.root_patch=patch.object(registration,'ROOT',self.root)
        self.operating_patch=patch.object(registration,'OPERATING_ID',self.operating)
        self.root_patch.start();self.operating_patch.start()
        self.payload={'client_id':'SEPARATE','client_secret':'PRIVATE-TEST-SECRET'}

    def tearDown(self):
        self.root_patch.stop();self.operating_patch.stop();self.temp.cleanup()

    def test_shared_id_is_rejected_and_no_file_is_written(self):
        with self.assertRaisesRegex(ValueError,'SHARED_OPERATING_CLIENT_FORBIDDEN'):
            registration.register({**self.payload,'client_id':'OPERATING'})
        self.assertEqual(list((self.root/'secrets').iterdir()),[])
        self.assertEqual(self.operating.read_text(),'OPERATING')

    def test_invalid_inputs_fail_before_writes(self):
        for payload in [None,[],{}, {'client_id':'x','client_secret':'a b'},
                        {'client_id':123,'client_secret':'x'}, {**self.payload,'extra':'x'},
                        {'client_id':'x'*513,'client_secret':'x'}]:
            with self.subTest(payload=payload),self.assertRaises(ValueError):registration.register(payload)
        self.assertEqual(list((self.root/'secrets').iterdir()),[])

    def test_existing_separate_auth_is_preserved_on_conflict(self):
        path=self.root/'secrets/dashboard-toss-client-secret'
        path.write_text('EXISTING-PRIVATE')
        with self.assertRaisesRegex(ValueError,'EXISTING_SEPARATE_AUTH_CONFLICT'):
            registration.register(self.payload)
        self.assertEqual(path.read_text(),'EXISTING-PRIVATE')
        self.assertFalse((self.root/'secrets/dashboard-toss-client-id').exists())

    def test_registration_writes_only_new_pair_and_returns_metadata(self):
        # POSIX ownership is checked on the Oracle host; Windows tests replace only
        # the two POSIX system calls while exercising real atomic file replacement.
        with patch.object(registration.os,'fchmod',create=True) as chmod,patch.object(registration.os,'fchown',create=True) as chown:
            result=registration.register(self.payload)
        self.assertEqual((self.root/'secrets/dashboard-toss-client-id').read_text().strip(),'SEPARATE')
        self.assertEqual((self.root/'secrets/dashboard-toss-client-secret').read_text().strip(),'PRIVATE-TEST-SECRET')
        self.assertEqual(self.operating.read_text(),'OPERATING')
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual(chmod.call_count,2);self.assertEqual(chown.call_count,2)
        self.assertEqual(chown.call_args.args[1:],(1001,1001))

    def test_parent_resolution_mismatch_is_rejected(self):
        with patch.object(Path,'resolve',return_value=self.root.parent/'unexpected'):
            with self.assertRaisesRegex(ValueError,'UNEXPECTED_SECRET_TARGET'):
                registration.register(self.payload)
        self.assertEqual(list((self.root/'secrets').iterdir()),[])

    def test_supplied_token_registration_and_explicit_replacement_use_only_new_file(self):
        with patch.object(registration.os,'fchmod',create=True),patch.object(registration.os,'fchown',create=True):
            result=registration.register({'access_token':'Bearer PRIVATE-TOKEN-ONE'})
            self.assertEqual((self.root/'secrets/dashboard-toss-access-token').read_text().strip(),'PRIVATE-TOKEN-ONE')
            replacement=registration.register({'access_token':'PRIVATE-TOKEN-TWO'})
        self.assertEqual(result['status'],'REGISTERED_SUPPLIED_ACCESS_TOKEN')
        self.assertFalse(result['token_reissued']);self.assertFalse(replacement['token_reissued'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertEqual((self.root/'secrets/dashboard-toss-access-token').read_text().strip(),'PRIVATE-TOKEN-TWO')
        self.assertEqual(self.operating.read_text(),'OPERATING')
        self.assertEqual(len(list((self.root/'secrets').iterdir())),1)

    def test_token_and_client_credentials_cannot_silently_replace_each_other(self):
        existing=self.root/'secrets/dashboard-toss-client-id'
        existing.write_text('EXISTING-CLIENT')
        with self.assertRaisesRegex(ValueError,'AUTH_MODE_CONFLICT'):
            registration.register({'access_token':'PRIVATE-TOKEN'})
        self.assertEqual(existing.read_text(),'EXISTING-CLIENT')
        self.assertFalse((self.root/'secrets/dashboard-toss-access-token').exists())
        existing.write_text('')
        token=self.root/'secrets/dashboard-toss-access-token'
        token.write_text('PRIVATE-TOKEN')
        with self.assertRaisesRegex(ValueError,'AUTH_MODE_CONFLICT'):registration.register(self.payload)
        self.assertEqual(token.read_text(),'PRIVATE-TOKEN')

    def test_invalid_supplied_token_is_rejected_before_writes(self):
        for token in ['',None,'Bearer ','a b','a\nheader','x'*8193,'cookie=value;cookie=other']:
            with self.subTest(token=str(token)[:20]),self.assertRaisesRegex(ValueError,'INVALID_AUTH_FORMAT'):
                registration.register({'access_token':token})
        self.assertEqual(list((self.root/'secrets').iterdir()),[])

    def test_failed_permissions_close_and_remove_staged_file(self):
        with patch.object(registration.os,'fchmod',create=True),patch.object(registration.os,'fchown',side_effect=OSError('ownership failed'),create=True):
            with self.assertRaises(OSError):registration.register({'access_token':'PRIVATE-TOKEN'})
        self.assertEqual(list((self.root/'secrets').iterdir()),[])
