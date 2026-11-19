from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from functools import partial
from io import StringIO
from itertools import count
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from stock_assistant.cli import main
from stock_assistant.daily_requests import DailyRefreshRequests
from stock_assistant.pipeline import CandidatePipeline
from stock_assistant.providers.http import JsonResponse, get_json
from stock_assistant.providers.krx import KRX_DAILY_ENDPOINTS, KRX_SECURITY_ENDPOINTS
from stock_assistant.providers.toss import TossMarketDataClient
from stock_assistant.repository import StockRepository
from test_daily_refresh import SYMBOLS, bar, calendar, security
from test_daily_requests import Response


NOW = datetime(2026, 10, 6, 3, tzinfo=timezone.utc)


class DailyRefreshCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / 'stock.sqlite3'
        self.repo = StockRepository(self.database)
        self.repo.save_securities([security(m) for m in SYMBOLS])
        self.repo.save_bars([bar(s, date(2026, 9, 23), NOW-timedelta(days=13)) for s in SYMBOLS.values()])
        self.args = ['refresh-daily', '--database', str(self.database)]
        for flag, name in (('--key-file', 'krx'), ('--client-id-file', 'id'), ('--client-secret-file', 'secret')):
            path = self.root / name
            path.write_text('synthetic-' + name, encoding='utf-8')
            self.args.extend((flag, str(path)))
        self.calls = []
        self.failures_remaining = 0
        self.sleep = Mock()

    def opener(self, request, *, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        endpoint = parsed.scheme + '://' + parsed.netloc + parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self.calls.append(endpoint)
        if endpoint == KRX_DAILY_ENDPOINTS['KOSDAQ'] and self.failures_remaining:
            self.failures_remaining -= 1
            raise TimeoutError('synthetic-private-value')
        if parsed.path.endswith('/market-calendar/KR'):
            payload = {'result': calendar(date.fromisoformat(query['date'][0]))}
        else:
            daily = endpoint in KRX_DAILY_ENDPOINTS.values()
            mapping = KRX_DAILY_ENDPOINTS if daily else KRX_SECURITY_ENDPOINTS
            market = next(m for m, url in mapping.items() if url == endpoint)
            symbol = SYMBOLS[market]
            if daily:
                row = {'BAS_DD': query['basDd'][0], 'ISU_CD': symbol, 'ISU_NM': market,
                       'TDD_OPNPRC': '10000', 'TDD_HGPRC': '10000', 'TDD_LWPRC': '10000',
                       'TDD_CLSPRC': '10000', 'ACC_TRDVOL': '100000'}
            else:
                row = {'ISU_SRT_CD': symbol, 'ISU_NM': market, 'ISU_ABBRV': market,
                       'KIND_STKCERT_TP_NM': '보통주', 'LIST_DD': '20200101'}
            payload = {'OutBlock_1': [row]}
        return Response(json.dumps(payload).encode())

    def run_cli(self, *, shared=False):
        requests = DailyRefreshRequests(fetch_json=partial(get_json, opener=self.opener), sleeper=self.sleep)
        clients = []

        def client_factory(client_id, client_secret, **kwargs):
            clients.append(dict(kwargs))
            kwargs["post_form"] = lambda *a, **k: JsonResponse(200, {'access_token': 'synthetic-token'})
            return TossMarketDataClient(client_id, client_secret, **kwargs)

        adapter = ModuleType('shared_toss_auth')
        adapter.SharedTossMarketDataClient = client_factory
        ticks = count()
        def now(*args):
            return NOW - timedelta(seconds=1) + timedelta(microseconds=next(ticks))

        with patch.dict('os.environ', {'STOCK_TOSS_AUTH_CACHE_DIR': 'fixture-cache' if shared else ''}), \
             patch.dict(sys.modules, {'shared_toss_auth': adapter}), \
             patch.object(sys, 'path', list(sys.path)), \
             patch('stock_assistant.cli.TossMarketDataClient', side_effect=client_factory), \
             patch('stock_assistant.cli.DailyRefreshRequests', return_value=requests), \
             patch('stock_assistant.cli.datetime', wraps=datetime) as cli_clock, \
             patch('stock_assistant.daily_refresh.datetime', wraps=datetime) as clock, \
             patch('sys.stdout', new_callable=StringIO) as output, \
             patch('sys.stderr', new_callable=StringIO) as errors:
            clock.now.side_effect = now
            cli_clock.now.side_effect = now
            code = main(self.args)
        return code, output.getvalue(), errors.getvalue(), clients

    def test_transient_market_timeout_recovers_through_actual_cli_and_clients(self):
        self.failures_remaining = 1
        code, output, errors, clients = self.run_cli()
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)['status'], 'READY')
        self.assertEqual(set(self.repo.daily_market_dates(as_of=NOW).values()), {'2026-10-02'})
        self.assertEqual(self.repo.latest_daily_check(as_of=NOW)['status'], 'READY')
        self.sleep.assert_called_once_with(2)
        event = json.loads(errors)
        self.assertEqual(event['request']['market'], 'KOSDAQ')
        self.assertEqual(event['status'], 'RETRYING')
        self.assertEqual(len(clients), 1)
        self.assertNotIn('synthetic-', errors)

    def test_exhausted_timeout_keeps_prices_unsaved_and_new_reports_blocked(self):
        self.failures_remaining = 3
        code, output, errors, _ = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertEqual(set(self.repo.daily_market_dates(as_of=NOW).values()), {'2026-09-23'})
        check = self.repo.latest_daily_check(as_of=NOW)
        self.assertEqual(check['status'], 'FAILED')
        self.assertEqual(check['request']['attempt'], 3)
        self.assertEqual(check['request']['phase'], 'CONNECTION_OR_HEADERS')
        failure = json.loads(errors.splitlines()[-1])
        self.assertEqual(failure['request'], check['request'])
        with self.assertRaisesRegex(ValueError, 'DAILY_DATA_NOT_READY'):
            CandidatePipeline(self.repo).run(as_of=NOW)
        self.assertEqual(self.repo.list_report_ids(), [])
        self.assertEqual(self.calls.count(KRX_DAILY_ENDPOINTS['KOSPI']), 1)
        self.assertEqual(self.calls.count(KRX_DAILY_ENDPOINTS['KOSDAQ']), 3)
        self.assertNotIn('synthetic-', errors + json.dumps(check))

    def test_shared_auth_adapter_receives_same_retry_transport(self):
        self.failures_remaining = 1
        code, output, errors, clients = self.run_cli(shared=True)
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)['status'], 'READY')
        self.assertIsInstance(clients[0]['fetch_json'].__self__, DailyRefreshRequests)
        self.assertIsInstance(clients[0]['post_form'].__self__, DailyRefreshRequests)
        self.sleep.assert_called_once_with(2)

    def test_actual_cli_continues_only_verified_latest_empty_data_as_pending(self):
        original=self.opener
        def empty_latest(request,*,timeout):
            query=urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            if request.full_url.startswith(KRX_DAILY_ENDPOINTS['ETF']) and query.get('basDd')==['20261002']:
                return Response(json.dumps({'OutBlock_1':[]}).encode())
            return original(request,timeout=timeout)
        self.opener=empty_latest
        self.args.append('--allow-unpublished-latest')
        code,output,errors,_=self.run_cli()
        self.assertEqual(code,0,errors)
        self.assertEqual(json.loads(output)['status'],'VERIFY_PENDING')
        self.assertEqual(self.repo.latest_daily_check(as_of=NOW)['status'],'FAILED')
        self.assertEqual(self.repo.latest_daily_check(as_of=NOW)['research_status'],'VERIFY_PENDING')
        self.assertEqual(set(self.repo.daily_market_dates(as_of=NOW).values()),{'2026-10-01'})


if __name__ == '__main__':
    unittest.main()
