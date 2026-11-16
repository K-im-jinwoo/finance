"""Display-only official-name overlay must preserve historical financial data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from stock_assistant.daytrade import build_trading_report
from stock_assistant.daytrade.ledger import DaytradeLedger
from stock_assistant.daytrade.stream import run_paper_ready_stream

FIXTURE=Path(__file__).with_name('fixtures')/'daytrade_observed_stream_sample.json'


class ReportNameTests(unittest.TestCase):
    def test_cached_names_cover_past_rows_without_changing_money_or_events(self):
        with tempfile.TemporaryDirectory() as root:
            original=run_paper_ready_stream(json.loads(FIXTURE.read_text(encoding='utf-8')),root)
            ledger=DaytradeLedger(root)
            try:
                before=list(ledger.connection.execute('SELECT payload FROM events ORDER BY sequence'))
                names={'005930':'삼성전자','000660':'SK하이닉스','035420':'NAVER'}
                with ledger.connection:ledger.connection.execute('INSERT INTO meta VALUES(?,?)',('symbol_names',json.dumps(names,ensure_ascii=False)))
                enriched=build_trading_report(ledger)
                self.assertEqual(enriched['symbol_names'],names)
                for key in ('fills','signals','decisions'):
                    for row in enriched[key]:
                        if row.get('symbol') in names:self.assertEqual(row['name'],names[row['symbol']])
                for scan in enriched['scan_history']:
                    for row in scan['selected']+scan['rejected']:
                        if row['symbol'] in names:self.assertEqual(row['name'],names[row['symbol']])
                self.assertEqual(enriched['account'],original['account'])
                self.assertEqual(before,list(ledger.connection.execute('SELECT payload FROM events ORDER BY sequence')))
                self.assertEqual(enriched['current_risk']['phase'],'CLOSED')
                self.assertFalse(enriched['current_risk']['entry_allowed'])
            finally:ledger.close()

    def test_quality_rows_have_the_issuer_name_and_invalid_cache_is_ignored(self):
        data=json.loads(FIXTURE.read_text(encoding='utf-8'))
        bad=next(r for r in data['events'] if r['kind']=='ws_frame' and r['frame']['topic']=='orderbook:kr:005930')
        bad['frame']['data']['bids'][0]['price']=bad['frame']['data']['asks'][0]['price']
        with tempfile.TemporaryDirectory() as root:
            run_paper_ready_stream(data,root);ledger=DaytradeLedger(root)
            try:
                with ledger.connection:ledger.connection.execute('INSERT INTO meta VALUES(?,?)',('symbol_names',json.dumps({'005930':'삼성전자','000660':12,'BAD':'wrong'})))
                report=build_trading_report(ledger)
                self.assertEqual(report['symbol_names'],{'005930':'삼성전자'})
                rows=[r for r in report['quality_events'] if r['symbol']=='005930']
                self.assertTrue(rows);self.assertEqual(rows[0]['name'],'삼성전자')
            finally:ledger.close()


if __name__=='__main__':unittest.main()
