import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine/src'))

def actor():
    namespace={}
    with patch('sys.stdin',io.StringIO('')),redirect_stdout(io.StringIO()):
        exec(compile((ROOT/'scripts/market_read_actor.py').read_text(encoding='utf-8'),'market_read_actor.py','exec'),namespace)
    return namespace

class MarketReaderTests(unittest.TestCase):
    def test_store_reads_latest_quote_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.sqlite3'
            with sqlite3.connect(path) as c:
                c.execute('CREATE TABLE market_quotes(symbol TEXT,observed_at TEXT,payload_json TEXT)')
                c.executemany('INSERT INTO market_quotes VALUES(?,?,?)',[('111111','2026-09-30T04:00:00+00:00',json.dumps({'last_price':'10'})),('111111','2026-09-30T04:05:00+00:00',json.dumps({'last_price':'12'}))])
            c.close()
            before=path.read_bytes()
            ns=actor();ns['Path']=lambda _:path
            result=ns['read_store'](['111111','222222'])
            self.assertEqual(result['quotes'],[{'last_price':'12'}])
            self.assertEqual(result['feed_mode'],'OPERATING_STORE_READ_ONLY')
            self.assertEqual(path.read_bytes(),before)

    def test_direct_refuses_collector_client_id_before_token_issue(self):
        ns=actor()
        ns['auth_mode']=lambda:'SEPARATE_CLIENT_CREDENTIALS'
        file=Mock();file.is_file.return_value=True;file.read_text.return_value='SAME-CLIENT'
        ns['Path']=Mock(return_value=file)
        with patch('stock_assistant.providers.toss.TossMarketDataClient') as client:
            with self.assertRaisesRegex(ValueError,'SHARED_OPERATING_TOKEN_FORBIDDEN'):ns['direct'](['111111'])
            client.assert_not_called()

    def test_direct_first_bar_uses_end_timestamp_raw_prices(self):
        ns=actor()
        start=datetime(2026,10,1,0,0,tzinfo=timezone.utc)
        observed=start+timedelta(seconds=90)
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return observed
        ns['datetime']=Clock
        ns['calendar_cache']={'date':'2026-10-01','regular_open':start.isoformat(),'observed_at':(start-timedelta(seconds=60)).isoformat(),'source':'OFFICIAL_TEST_CALENDAR'}
        client=Mock();client._token.return_value='PRIVATE-TEST-TOKEN';client.prices.return_value=[]
        client.auth_mode='SEPARATE_CLIENT_CREDENTIALS'
        ns['client']=client
        payload={'result':{'candles':[{'timestamp':(start+timedelta(minutes=1)).isoformat(),'openPrice':'100','highPrice':'110','lowPrice':'99','closePrice':'105','volume':'20','currency':'KRW'}]}}
        with patch('stock_assistant.providers.http.get_json',return_value=Mock(payload=payload)) as fetch:
            result=ns['direct'](['111111'])
        self.assertEqual(result['opened']['prices'],{'111111':'100'})
        self.assertEqual(result['opened']['bar_end'],(start+timedelta(minutes=1)).isoformat())
        self.assertEqual(fetch.call_args.kwargs['query']['adjusted'],'false')
        self.assertEqual(fetch.call_args.kwargs['query']['before'],(start+timedelta(minutes=1)).isoformat())
        self.assertNotIn('PRIVATE-TEST-TOKEN',json.dumps(result))

    def test_supplied_token_reads_public_data_without_issuing_or_returning_token(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            (folder/'dashboard-toss-access-token').write_text('PRIVATE-ACCESS-TOKEN')
            ns=actor();ns['Path']=lambda path:folder/Path(path).name
            quote={'symbol':'240810','lastPrice':'100','currency':'KRW','timestamp':datetime.now(timezone.utc).isoformat()}
            calendar={'result':{'today':{'integrated':None},'nextBusinessDay':{'integrated':None}}}
            with patch('stock_assistant.providers.http.get_json',side_effect=[Mock(payload=calendar),Mock(payload={'result':[quote]})]) as fetch,patch('stock_assistant.providers.toss.TossMarketDataClient') as issuing_client,patch('stock_assistant.providers.toss.post_form_json') as issue:
                result=ns['direct'](['240810'])
            self.assertEqual(result['auth_mode'],'SUPPLIED_ACCESS_TOKEN')
            self.assertEqual(result['quotes'][0]['symbol'],'240810')
            self.assertNotIn('PRIVATE-ACCESS-TOKEN',json.dumps(result))
            self.assertTrue(all(call.args[0].startswith('https://openapi.tossinvest.com/api/v1/') for call in fetch.call_args_list))
            issuing_client.assert_not_called();issue.assert_not_called()

    def test_supplied_token_replacement_is_read_without_process_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'token'
            path.write_text('TOKEN-ONE')
            client=actor()['SuppliedAccessTokenClient'](path)
            self.assertEqual(client._token(),'TOKEN-ONE')
            path.write_text('TOKEN-TWO')
            self.assertEqual(client._token(),'TOKEN-TWO')
            for invalid in ['', 'a b','bad\nheader','x'*8193]:
                path.write_text(invalid)
                with self.subTest(invalid=invalid[:20]),self.assertRaisesRegex(ValueError,'INVALID_SUPPLIED_ACCESS_TOKEN'):
                    client._token()

