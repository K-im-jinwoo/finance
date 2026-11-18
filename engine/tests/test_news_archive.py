from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from dataclasses import replace
import io
import json
import tempfile
from pathlib import Path
import unittest
import zipfile

from stock_assistant.news_archive import collect_archive
from stock_assistant.official_events import contract_fields
from stock_assistant.providers.news import NewsArticle, NewsPage, NaverNewsClient
from stock_assistant.providers.http import JsonResponse
from stock_assistant.research_data import ResearchData
from stock_assistant.repository import StockRepository
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.dart_research import DartResearchCollector
from stock_assistant.http_api import StockApi
from stock_assistant.material_archive import collect_material_archive
from tests.test_dart_research import FakeDart
from tests.test_screening import AS_OF, samsung


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.sqlite3'
        repo = StockRepository(self.source); repo.save_securities([samsung()])
        self.repo = ReadOnlyResearchRepository(self.source)
        self.store = ResearchData(Path(self.tmp.name) / 'study.sqlite3', source_database=self.source)
        DartResearchCollector(self.repo, self.store, FakeDart(), clock=lambda:AS_OF, sleeper=lambda _:None).run()

    def article(self, number, days=1):
        return NewsArticle('삼성전자 기사 ' + str(number), 'https://example.test/' + str(number), AS_OF-timedelta(days=days), AS_OF)

    def add_issuers(self, symbols):
        repo = StockRepository(self.source)
        bundle = self.store.latest('financial_bundle', '005930', as_of=AS_OF)['payload']
        cursor = self.store.latest('dart_cursor', '005930', as_of=AS_OF)['payload']
        for symbol in symbols:
            repo.save_securities([replace(samsung(), symbol=symbol)])
            self.store.put('financial_bundle', symbol, json.loads(json.dumps(bundle).replace('005930', symbol)), observed_at=AS_OF)
            self.store.put('dart_cursor', symbol, cursor, observed_at=AS_OF)

    def test_five_call_batch_resumes_two_issuers_and_starts_three_without_extra_reservation(self):
        self.add_issuers(['000001', '000002', '000003', '000004', '000005'])
        for symbol in ['005930', '000001']:
            self.store.put('archive_cursor', symbol, {'start': '2023-09-19', 'end': '2026-09-19',
                'status': 'PARTIAL', 'next_start': 100, 'anchor_url': 'https://example.test/99'}, observed_at=AS_OF)
        client = Mock()
        client.archive_page.side_effect = lambda **kw: NewsPage(tuple(self.article(i) for i in range(100)),
                                                               10000, kw['start'], 100, AS_OF)
        result = collect_archive(self.repo, self.store, client, pages=5, clock=lambda: AS_OF)
        self.assertEqual(result['actual_calls'], 5)
        self.assertEqual(result['reserved_calls'], 5)
        self.assertEqual([r['symbol'] for r in result['results']], ['000001', '005930', '000002', '000003', '000004'])
        self.assertEqual([c.kwargs['start'] for c in client.archive_page.call_args_list], [100, 100, 1, 1, 1])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertFalse(result['history_review_complete'])
        self.assertEqual(self.store.latest_by_symbol('news_coverage', as_of=AS_OF), {})
        client.archive_batch.assert_called_once_with(5)
        later = AS_OF + timedelta(minutes=1)
        again = collect_archive(self.repo, self.store, client, pages=5, clock=lambda: later)
        self.assertIn('000005', [r['symbol'] for r in again['results']])

    def test_failure_in_later_target_keeps_earlier_cursor_and_does_not_certify_history(self):
        self.add_issuers(['000001'])
        self.store.put('archive_cursor', '005930', {'start': '2023-09-19', 'end': '2026-09-19',
            'status': 'PARTIAL', 'next_start': 100, 'anchor_url': 'https://example.test/99'}, observed_at=AS_OF)
        client = Mock()
        client.archive_page.side_effect = [NewsPage(tuple(self.article(i) for i in range(100)), 10000, 100, 100, AS_OF),
                                           ValueError('invalid page')]
        with self.assertRaises(ValueError):
            collect_archive(self.repo, self.store, client, pages=2, clock=lambda: AS_OF)
        self.assertEqual(self.store.latest('archive_cursor', '005930', as_of=AS_OF)['payload']['next_start'], 199)
        self.assertIsNone(self.store.latest('archive_cursor', '000001', as_of=AS_OF))
        self.assertEqual(self.store.latest_by_symbol('news_coverage', as_of=AS_OF), {})
        client.archive_batch.assert_called_once_with(2)
        self.assertEqual(client.archive_page.call_count, 2)

    def test_multiple_exhausted_searches_share_one_reservation_and_keep_per_issuer_coverage(self):
        self.add_issuers(['000001', '000002'])
        client = Mock()
        client.archive_page.return_value = NewsPage((), 0, 1, 0, AS_OF)
        result = collect_archive(self.repo, self.store, client, pages=5, clock=lambda: AS_OF)
        self.assertEqual(result['actual_calls'], 3)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(set(self.store.latest_by_symbol('news_coverage', as_of=AS_OF)), {'000001', '000002', '005930'})
        client.archive_batch.assert_called_once_with(5)

    def test_exhaustion_creates_scoped_auto_coverage_and_true_observation(self):
        client=Mock(); client.archive_page.return_value=NewsPage((self.article(1, 800),),1,1,1,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(self.store.latest('news_coverage','005930',as_of=AS_OF)['payload']['status'],'AUTO_SEARCH_VERIFIED')
        client.archive_batch.assert_called_once_with(1)

    def test_oldest_reaches_cutoff_without_provider_exhaustion(self):
        client=Mock(); rows=tuple(self.article(i,1099+i) for i in range(100))
        client.archive_page.return_value=NewsPage(rows,10000,1,100,AS_OF)
        self.assertEqual(collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)['status'],'COMPLETE')

    def test_partial_resume_overlap_shift_restarts_instead_of_silently_skipping(self):
        client=Mock(); rows=tuple(self.article(i,i) for i in range(100))
        client.archive_page.return_value=NewsPage(rows,10000,1,100,AS_OF)
        first=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(first['next_start'],100)
        client.archive_page.return_value=NewsPage((self.article(999,150),),10000,100,1,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['pagination_restarts'],1)
        self.assertEqual(result['status'],'PARTIAL')
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))

    def test_provider_cap_is_never_neutral_coverage(self):
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
            'next_start':901,'anchor_url':'https://example.test/0'},observed_at=AS_OF)
        client=Mock(); client.archive_page.return_value=NewsPage(tuple(self.article(i) for i in range(100)),5000,901,100,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['status'],'PROVIDER_LIMIT')
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))

    def test_provider_failure_does_not_advance_cursor(self):
        client=Mock(); client.archive_page.side_effect=ValueError('invalid page')
        with self.assertRaises(ValueError):collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertIsNone(self.store.latest('archive_cursor','005930',as_of=AS_OF))

    def test_provider_reservation_required_metadata_and_future_dates_rejected(self):
        fetch=Mock(return_value=JsonResponse(200,{'total':0,'start':1,'items':[]}))
        client=NaverNewsClient('fixture-id','fixture-secret',fetch_json=fetch,budget=Mock(),clock=lambda:AS_OF)
        with self.assertRaises(ValueError):client.archive_page(query='삼성전자',start=1)
        client.archive_batch(1); self.assertEqual(client.archive_page(query='삼성전자',start=1).raw_count,0)
        with self.assertRaises(ValueError):client.archive_page(query='삼성전자',start=2)
        client.archive_batch(1); fetch.return_value=JsonResponse(200,{'total':0,'start':2,'items':[]})
        with self.assertRaises(Exception):client.archive_page(query='삼성전자',start=1)

    def test_read_only_research_get_does_not_write_or_change_legacy_report(self):
        report={'report_id':'UR-'+'a'*24,'contract':'UNIFIED_RESEARCH_V1','paper_account_eligible':False}
        self.store.put('unified_report','ALL',report,observed_at=AS_OF)
        api=StockApi(self.repo,role_secrets={'cio':'fixture','risk':'risk-fixture'})
        with patch.dict('os.environ',{'STOCK_UNIFIED_RESEARCH_DATABASE':str(self.store.path)}):
            result=api.dispatch('GET','/v1/unified/reports/latest',headers={'Authorization':'Bearer fixture'})
            self.assertEqual(result.status,200)
            self.assertEqual(result.body['report'],report)
            self.assertEqual(api.dispatch('POST','/v1/unified/reports/latest',headers={'Authorization':'Bearer risk-fixture'}).status,403)
        readonly=ResearchData(self.store.path,source_database=self.source,read_only=True)
        with self.assertRaises(PermissionError):readonly.put('unified_report','ALL',report,observed_at=AS_OF)

    def test_material_queries_supplement_history_without_certifying_broad_news(self):
        client=Mock();client.archive_page.return_value=NewsPage((),0,1,0,AS_OF)
        result=collect_material_archive(self.repo,self.store,client,pages=5,clock=lambda:AS_OF)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertIsNotNone(self.store.latest('material_coverage','005930',as_of=AS_OF))
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))

    def test_partial_archive_keeps_original_frontier_across_dates(self):
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
            'next_start':100,'anchor_url':'https://example.test/99'},observed_at=AS_OF)
        client=Mock();client.archive_page.return_value=NewsPage((self.article(99,100),),100,100,1,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF+timedelta(days=1))
        self.assertEqual(result['status'],'COMPLETE')
        self.assertEqual(self.store.latest('news_coverage','005930',as_of=AS_OF+timedelta(days=1))['payload']['end'],'2026-09-19')

    def test_pending_issuer_is_resumed_before_a_new_issuer(self):
        repo=StockRepository(self.source);repo.save_securities([replace(samsung(),symbol='000001')])
        DartResearchCollector(self.repo,self.store,FakeDart(),clock=lambda:AS_OF,sleeper=lambda _:None).run()
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
                      'next_start':100,'anchor_url':'https://example.test/99'},observed_at=AS_OF)
        client=Mock();client.archive_page.return_value=NewsPage((self.article(99),),100,100,1,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['symbol'],'005930')
        self.assertEqual(client.archive_page.call_args.kwargs['start'],100)

    def test_small_index_shift_keeps_overlap_and_advances_without_a_restart(self):
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
                     'next_start':100,'anchor_url':'https://example.test/99'},observed_at=AS_OF)
        rows=(self.article(1),self.article(99),*(self.article(i) for i in range(100,198)))
        client=Mock();client.archive_page.return_value=NewsPage(rows,2000,100,100,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['pagination_restarts'],0)
        self.assertEqual(result['next_start'],199)

    def test_capped_search_does_not_restart_the_same_day_or_on_every_new_day(self):
        self.store.put('archive_cursor','005930',{'status':'PROVIDER_LIMIT','end':'2026-09-19'},observed_at=AS_OF)
        client=Mock()
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF+timedelta(days=1))
        self.assertEqual(result['status'],'CURRENT')
        client.archive_batch.assert_not_called()

    def test_larger_index_shift_seeks_the_anchor_without_certifying_a_gap(self):
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
            'next_start':100,'anchor_url':'https://example.test/anchor','oldest_seen':(AS_OF-timedelta(days=100)).isoformat()},observed_at=AS_OF)
        newer=tuple(self.article(i,10+i//10) for i in range(100))
        anchored=(replace(self.article(1,100),url='https://example.test/anchor'),*(self.article(i,100+i) for i in range(1,100)))
        client=Mock();client.archive_page.side_effect=[NewsPage(newer,5000,100,100,AS_OF),NewsPage(anchored,5000,199,100,AS_OF)]
        result=collect_archive(self.repo,self.store,client,pages=2,clock=lambda:AS_OF)
        self.assertEqual(result['next_start'],298)
        self.assertEqual(result['pagination_restarts'],0)
        self.assertEqual([c.kwargs['start'] for c in client.archive_page.call_args_list],[100,199])
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))

    def test_anchor_outside_accessible_window_is_a_limit_and_not_neutral_history(self):
        self.store.put('archive_cursor','005930',{'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL',
            'next_start':901,'anchor_url':'https://example.test/old','oldest_seen':(AS_OF-timedelta(days=100)).isoformat()},observed_at=AS_OF)
        client=Mock();client.archive_page.return_value=NewsPage(tuple(self.article(i) for i in range(100)),5000,901,100,AS_OF)
        result=collect_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(result['status'],'PROVIDER_LIMIT')
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))

    def test_one_capped_risk_query_does_not_stop_the_other_four(self):
        state={'start':'2023-09-19','end':'2026-09-19','status':'PARTIAL','queries':[
            {'keyword':'횡령','category':'MANAGEMENT','next_start':901,'anchor_url':None,'status':'PARTIAL'},
            {'keyword':'배임','category':'MANAGEMENT','next_start':1,'anchor_url':None,'status':'PARTIAL'}]}
        self.store.put('material_archive_cursor','005930',state,observed_at=AS_OF)
        client=Mock();client.archive_page.side_effect=[NewsPage(tuple(self.article(i) for i in range(100)),2000,901,100,AS_OF),
                                                     NewsPage((),0,1,0,AS_OF)]
        first=collect_material_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(first['status'],'PARTIAL')
        second=collect_material_archive(self.repo,self.store,client,pages=1,clock=lambda:AS_OF)
        self.assertEqual(second['status'],'PROVIDER_LIMIT')
        self.assertTrue(client.archive_page.call_args.kwargs['query'].endswith('배임'))
        self.assertIsNone(self.store.latest('news_coverage','005930',as_of=AS_OF))


class ContractParserTests(unittest.TestCase):
    def bundle(self,xml):
        file=io.BytesIO()
        with zipfile.ZipFile(file,'w') as archive:archive.writestr('report.xml',xml)
        return file.getvalue()

    def test_primary_structured_fields_parse_without_using_news_claims(self):
        rows={'계약상대방':'검증기업','체결계약명':'제품 공급','매출액대비(%)':'12.5','시작일':'2026-08-01',
              '종료일':'2027-08-01','확정 계약금액':'10,000'}
        xml='<ROOT>'+''.join('<TR><TD>'+k+'</TD><TD>'+v+'</TD></TR>' for k,v in rows.items())+'</ROOT>'
        result=contract_fields(self.bundle(xml.encode()))
        self.assertEqual(str(result['ratio']),'12.5')
        self.assertEqual(result['end'].year,2027)

    def test_missing_fields_and_entity_injection_are_rejected(self):
        with self.assertRaises(ValueError):contract_fields(self.bundle(b'<ROOT/>'))
        with self.assertRaises(ValueError):contract_fields(self.bundle(b'<!ENTITY bad SYSTEM "file:///secret"><ROOT/>'))

    def test_real_provider_html_shape_contract_counterparty_and_unclosed_br(self):
        rows=[['- 체결계약명','MLCC 공급계약'],['2. 계약내역','계약금액(원)','285,611,040,000'],
              ['매출액대비(%)','2.5'],['3. 계약상대','글로벌 대형기업'],['5. 계약기간','시작일','2027-01-01'],['종료일','2027-12-31']]
        html='<html><head><meta charset="UTF-8"></head><body><br><table>'+''.join('<tr>'+''.join('<td>'+cell+'</td>' for cell in row)+'</tr>' for row in rows)+'</table></body></html>'
        parsed=contract_fields(self.bundle(html.encode()))
        self.assertEqual(parsed['party'],'글로벌 대형기업')
        self.assertEqual(str(parsed['ratio']),'2.5')

if __name__=='__main__':unittest.main()
