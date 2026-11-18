from __future__ import annotations

import json
import unittest
import urllib.error
from functools import partial
from unittest.mock import Mock

from stock_assistant.daily_requests import DailyRefreshRequests
from stock_assistant.providers.http import (
    AuthenticationError, NetworkError, RateLimitError, UpstreamSchemaError, get_json,
)
from stock_assistant.providers.krx import KRX_DAILY_ENDPOINTS
from stock_assistant.providers.toss import TOSS_BASE_URL


URL = KRX_DAILY_ENDPOINTS['KOSDAQ']
QUERY = {'basDd': '20261006'}


class Response:
    status = 200

    def __init__(self, body=b'{"OutBlock_1": []}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class DailyRequestsTests(unittest.TestCase):
    def setUp(self):
        self.sleep = Mock()
        self.events = []

    def requests(self, opener):
        return DailyRefreshRequests(fetch_json=partial(get_json, opener=opener),
                                    sleeper=self.sleep, diagnostic=self.events.append)

    def test_normal_request_has_no_retry_or_diagnostic(self):
        opener = Mock(return_value=Response())
        result = self.requests(opener).get_json(URL, query=QUERY)
        self.assertEqual(result.payload, {'OutBlock_1': []})
        opener.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.events, [])
        self.assertEqual(opener.call_args.kwargs['timeout'], 15)

    def test_header_timeout_recovers_only_the_failed_request(self):
        opener = Mock(side_effect=[TimeoutError('fixture'), Response()])
        result = self.requests(opener).get_json(URL, query=QUERY, headers={'AUTH_KEY': 'synthetic-key'})
        self.assertEqual(result.status, 200)
        self.assertEqual(opener.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.assertEqual(self.events[0]['status'], 'RETRYING')
        self.assertEqual(self.events[0]['request']['phase'], 'CONNECTION_OR_HEADERS')
        self.assertEqual(self.events[0]['request']['market'], 'KOSDAQ')
        self.assertEqual(self.events[0]['request']['request_date'], '2026-10-06')
        self.assertEqual(opener.call_args_list[0].args[0].full_url, opener.call_args_list[1].args[0].full_url)

    def test_body_timeout_is_distinguished_and_recovered(self):
        opener = Mock(side_effect=[Response(TimeoutError('body fixture')), Response()])
        self.requests(opener).get_json(URL, query=QUERY)
        self.assertEqual(self.events[0]['request']['phase'], 'RESPONSE_BODY')
        self.assertEqual(self.events[0]['request']['http_status'], 200)
        self.assertEqual(opener.call_count, 2)

    def test_wrapped_socket_timeout_is_retried(self):
        opener = Mock(side_effect=[urllib.error.URLError(TimeoutError('fixture')), Response()])
        self.requests(opener).get_json(URL, query=QUERY)
        self.assertEqual(opener.call_count, 2)

    def test_three_timeouts_fail_with_terminal_request_context(self):
        opener = Mock(side_effect=TimeoutError('fixture'))
        with self.assertRaises(NetworkError) as raised:
            self.requests(opener).get_json(URL, query=QUERY)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [2, 5])
        self.assertEqual([e['status'] for e in self.events], ['RETRYING', 'RETRYING', 'FAILED'])
        context = raised.exception.request_context
        self.assertEqual(context['attempt'], 3)
        self.assertEqual(context['max_attempts'], 3)
        self.assertEqual(context['provider'], 'KRX_OPEN_API')
        self.assertGreaterEqual(context['elapsed_seconds'], 0)

    def test_http_errors_are_never_retried(self):
        for status, error_type in ((401, AuthenticationError), (403, AuthenticationError),
                                   (429, RateLimitError), (503, NetworkError)):
            with self.subTest(status=status):
                opener = Mock(side_effect=urllib.error.HTTPError(URL, status, 'fixture', {}, None))
                with self.assertRaises(error_type) as raised:
                    self.requests(opener).get_json(URL, query=QUERY)
                opener.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(raised.exception.request_context['phase'], 'RESPONSE_STATUS')
                self.assertEqual(raised.exception.request_context['http_status'], status)

    def test_dns_error_and_timeout_text_without_real_cause_are_not_retried(self):
        opener = Mock(side_effect=urllib.error.URLError('name not found'))
        with self.assertRaises(NetworkError):
            self.requests(opener).get_json(URL, query=QUERY)
        opener.assert_called_once()
        fetch = Mock(side_effect=NetworkError('provider request failed: TimeoutError'))
        with self.assertRaises(NetworkError):
            DailyRefreshRequests(fetch_json=fetch, sleeper=self.sleep,
                                 diagnostic=self.events.append).get_json(URL, query=QUERY)
        fetch.assert_called_once()
        self.sleep.assert_not_called()

    def test_invalid_json_and_non_object_json_fail_immediately(self):
        for body in (b'not json', b'[]', b'\xff'):
            with self.subTest(body=body):
                opener = Mock(return_value=Response(body))
                with self.assertRaises(UpstreamSchemaError) as raised:
                    self.requests(opener).get_json(URL, query=QUERY)
                opener.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(raised.exception.request_context['phase'], 'JSON_DECODE')

    def test_diagnostics_exclude_credentials_query_values_and_raw_errors(self):
        secret = 'synthetic-secret-value'
        opener = Mock(side_effect=TimeoutError(secret))
        with self.assertRaises(NetworkError) as raised:
            self.requests(opener).get_json(URL, query={**QUERY, 'secret': secret},
                                           headers={'AUTH_KEY': secret, 'Authorization': 'Bearer ' + secret})
        serialized = json.dumps(self.events) + json.dumps(raised.exception.request_context)
        self.assertNotIn(secret, serialized)
        self.assertNotIn('AUTH_KEY', serialized)
        self.assertNotIn('Bearer', serialized)
        self.assertNotIn('?', raised.exception.request_context['endpoint'])

    def test_invalid_dates_and_other_endpoints_fail_before_transport(self):
        cases = [(URL, {'basDd': '20260230'}), (URL, {'basDd': 'secret'}),
                 (URL, {}), (TOSS_BASE_URL + '/oauth2/token', {'date': '2026-10-06'}),
                 (TOSS_BASE_URL + '/api/v1/market-calendar/KR', {'date': '20261006'}),
                 (URL + '?token=synthetic-secret', QUERY)]
        for url, query in cases:
            with self.subTest(url=url, query=query):
                opener = Mock()
                with self.assertRaises(ValueError):
                    self.requests(opener).get_json(url, query=query)
                opener.assert_not_called()
        self.assertEqual(self.events, [])

    def test_generic_get_json_keeps_single_attempt_and_original_error_contract(self):
        opener = Mock(side_effect=TimeoutError('synthetic-secret'))
        with self.assertRaisesRegex(NetworkError, '^provider request failed: TimeoutError$'):
            get_json(URL, query=QUERY, opener=opener)
        opener.assert_called_once()


if __name__ == '__main__':
    unittest.main()
