import json
import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
import urllib.error

spec = importlib.util.spec_from_file_location('news_registration', Path(__file__).resolve().parents[1] / 'scripts/register_oracle_news_auth.py')
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)
register = registration.register


class NaverRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def save(self, payload, verifier=None):
        with patch.object(registration.os, 'fchmod', create=True), \
             patch.object(registration.os, 'fchown', create=True):
            return register(payload, root=self.root, verifier=verifier or (lambda values: None))

    def test_valid_idempotent_and_no_credentials_in_response(self):
        payload = {'client_id': 'fixture-news-id', 'client_secret': 'fixture-news-secret'}
        first = self.save(payload)
        self.assertEqual(self.save(payload), first)
        self.assertEqual((self.root / 'naver-news-client-id').read_text().strip(), payload['client_id'])
        self.assertEqual((self.root / 'naver-news-client-secret').read_text().strip(), payload['client_secret'])
        self.assertNotIn('fixture-news', json.dumps(first))
        self.assertFalse(first['news_enabled'])
        self.assertTrue(first['news_auth_verified'])
        self.assertEqual(first['messages_sent'], 0)
        self.assertFalse(list(self.root.glob('.naver-auth-*')))

    def test_invalid_shape_and_header_injection_do_not_write(self):
        for payload in ({}, {'client_id': 'a'}, {'client_id': '', 'client_secret': 's'},
                        {'client_id': 'a\nheader', 'client_secret': 's'},
                        {'client_id': 'a', 'client_secret': 's' * 513},
                        {'client_id': 123, 'client_secret': 's'},
                        {'client_id': 'a', 'client_secret': 's', 'enable': True}):
            with self.subTest(payload=repr(payload)[:60]), self.assertRaises(ValueError):
                self.save(payload)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_different_secret_is_preserved_before_any_write(self):
        target = self.root / 'naver-news-client-secret'
        target.write_text('existing-fixture-secret')
        with self.assertRaisesRegex(ValueError, 'EXISTING_AUTH_CONFLICT'):
            self.save({'client_id': 'new-fixture-id', 'client_secret': 'new-fixture-secret'})
        self.assertEqual(target.read_text(), 'existing-fixture-secret')
        self.assertFalse((self.root / 'naver-news-client-id').exists())

    def test_failed_new_auth_does_not_save_or_replace(self):
        self.save({'client_id': 'old-fixture-id', 'client_secret': 'old-fixture-secret'})
        verifier = Mock(side_effect=ValueError('NAVER_AUTH_REJECTED'))
        with self.assertRaisesRegex(ValueError, 'NAVER_AUTH_REJECTED'):
            self.save({'client_id': 'new-fixture-id', 'client_secret': 'new-fixture-secret'}, verifier)
        self.assertEqual((self.root / 'naver-news-client-id').read_text().strip(), 'old-fixture-id')
        self.assertFalse(list(self.root.glob('.naver-auth-*')))

    def test_only_verified_input_can_replace_rejected_existing_auth(self):
        self.save({'client_id': 'old-fixture-id', 'client_secret': 'old-fixture-secret'})
        verifier = Mock(side_effect=[None, ValueError('NAVER_AUTH_REJECTED')])
        result = self.save({'client_id': 'new-fixture-id', 'client_secret': 'new-fixture-secret'}, verifier)
        self.assertTrue(result['replaced_invalid_auth'])
        self.assertEqual(verifier.call_count, 2)
        self.assertEqual((self.root / 'naver-news-client-secret').read_text().strip(), 'new-fixture-secret')

    def test_valid_existing_auth_and_network_uncertainty_are_preserved(self):
        self.save({'client_id': 'old-fixture-id', 'client_secret': 'old-fixture-secret'})
        for failure in (None, ValueError('NAVER_RATE_LIMITED'), ValueError('NAVER_CHECK_UNAVAILABLE')):
            verifier = Mock(side_effect=[None, failure])
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                self.save({'client_id': 'new-fixture-id', 'client_secret': 'new-fixture-secret'}, verifier)
        self.assertEqual((self.root / 'naver-news-client-id').read_text().strip(), 'old-fixture-id')

    def test_active_feature_credentials_are_preserved(self):
        (self.root / 'schedule.env').write_text('export STOCK_NEWS_ENABLED=true\n')
        verifier = Mock()
        with self.assertRaisesRegex(ValueError, 'ACTIVE_NEWS_AUTH_REQUIRES_CUTOVER'):
            self.save({'client_id': 'fixture-id', 'client_secret': 'fixture-secret'}, verifier)
        verifier.assert_not_called()

    def test_naver_endpoint_headers_and_safe_auth_errors(self):
        cost_guard = patch.object(registration, 'reserve_news_auth_call')
        cost_guard.start()
        self.addCleanup(cost_guard.stop)
        credentials = {'client_id': 'fixture-id', 'client_secret': 'fixture-secret'}
        response = Mock(status=200)
        response.read.return_value = b'{"items":[]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(registration.urllib.request, 'urlopen', return_value=response) as opener:
            registration.verify_credentials(credentials)
        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://naverapihub.apigw.ntruss.com/search/v1/news?'))
        self.assertEqual(request.get_header('X-ncp-apigw-api-key-id'), 'fixture-id')
        self.assertEqual(request.get_header('X-ncp-apigw-api-key'), 'fixture-secret')
        self.assertIsNone(request.get_header('X-naver-client-id'))
        for status, code in ((401, 'NAVER_AUTH_REJECTED'), (403, 'NAVER_SEARCH_PERMISSION_REQUIRED'),
                             (429, 'NAVER_RATE_LIMITED'), (500, 'NAVER_CHECK_UNAVAILABLE')):
            error = urllib.error.HTTPError(request.full_url, status, 'fixture-private-error', {}, None)
            with self.subTest(status=status), patch.object(registration.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaisesRegex(ValueError, '^' + code + '$'):
                    registration.verify_credentials(credentials)

    def test_cost_guard_refusal_prevents_auth_api_request(self):
        with patch.object(registration, 'reserve_news_auth_call', side_effect=ValueError('NEWS_COST_LIMIT_REACHED')), \
             patch.object(registration.urllib.request, 'urlopen') as opener:
            with self.assertRaisesRegex(ValueError, 'NEWS_COST_LIMIT_REACHED'):
                registration.verify_credentials({'client_id': 'fixture-id', 'client_secret': 'fixture-secret'})
            opener.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions require Linux')
    def test_linux_permissions_and_symlink_rejection(self):
        register({'client_id': 'fixture-id', 'client_secret': 'fixture-secret'},
                 root=self.root, owner=(os.getuid(), os.getgid()), verifier=lambda values: None)
        target = self.root / 'naver-news-client-id'
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        target.unlink()
        target.symlink_to(self.root / 'naver-news-client-secret')
        with self.assertRaisesRegex(ValueError, 'UNEXPECTED_SECRET_TARGET'):
            register({'client_id': 'fixture-id', 'client_secret': 'fixture-secret'},
                     root=self.root, owner=(os.getuid(), os.getgid()), verifier=lambda values: None)


if __name__ == '__main__':
    unittest.main()
