"""Offline operating-cycle regressions: price delay cannot halt research or authorize buys."""
from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from stock_assistant.research_data import ResearchData
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.repository import StockRepository
from stock_assistant.providers.news import NewsPage
from stock_assistant.providers.http import RateLimitError
from stock_assistant.unified_cycle import refresh_unified
from stock_assistant.unified_research import render_recommendations
from tests.helpers import make_bars
from tests.test_dart_research import FakeDart
from tests.test_screening import AS_OF,samsung
from tests.test_split_news_policy import completed_risk_state
from stock_assistant.news_policy import RISK_QUERY_VERSION


class OperatingCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source=Path(self.tmp.name)/'source.sqlite3'
        self.repo=StockRepository(self.source);self.repo.save_securities([samsung()])
        from datetime import date
        self.repo.save_bars(make_bars(start=date(2026,7,2)))
        self.reader=ReadOnlyResearchRepository(self.source)
        self.store=ResearchData(Path(self.tmp.name)/'research.sqlite3',source_database=self.source)
        self.news=Mock();self.news.archive_page.return_value=NewsPage((),0,1,0,AS_OF)
        self.calendar=Mock();self.calendar.calendar.return_value={
            'today':{'date':'2026-09-20','integrated':{}},
            'previousBusinessDay':{'date':'2026-09-19','integrated':{'regularMarket':True}}}

    def run_cycle(self):
        with patch('stock_assistant.unified_cycle.review_official_contracts',return_value={'verified':[],'pending':[]}):
            return refresh_unified(self.reader,self.store,dart_client=FakeDart(),news_client=self.news,
                    calendar_client=self.calendar,dart_key='fixture-only',archive_pages=1,clock=lambda:AS_OF)

    def test_latest_price_pending_still_collects_and_scores_but_blocks_buy(self):
        self.store.put('risk_archive_cursor', '005930', completed_risk_state(), observed_at=AS_OF)
        self.repo.save_daily_check({'status':'FAILED','research_status':'VERIFY_PENDING',
          'checked_at':(AS_OF-timedelta(seconds=1)).isoformat(),'target_date':'2026-09-19',
          'error_code':'KRX_DAILY_DATA_UNAVAILABLE'})
        before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        report=self.run_cycle()
        self.assertEqual(report['refresh_stages']['financials']['results'][0]['status'],'COLLECTED')
        self.assertEqual(report['refresh_stages']['archive']['status'],'COMPLETE')
        self.assertEqual(report['refresh_stages']['prices']['status'],'VERIFY_PENDING')
        row=report['evaluations'][0]
        self.assertEqual(row['news_score'],'50')
        self.assertEqual(row['total_score'],'85.0')
        self.assertEqual(row['action_decision'],'VERIFY_PENDING')
        self.assertIn('PRICE_REFRESH_UNVERIFIED',row['pending_checks'])
        self.assertEqual(report['candidates'],[])
        self.assertIn('가격 자료: 확인 대기',render_recommendations(report))
        self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.repo.save_daily_check({'status':'READY','checked_at':AS_OF.isoformat(),'target_date':'2026-09-19'})
        recovered=self.run_cycle()
        self.assertEqual(recovered['candidates'][0]['action_decision'],'BUY_RECOMMENDATION')

    def test_finished_capped_material_queries_allow_the_next_issuer_archive(self):
        self.repo.save_securities([replace(samsung(),symbol='000001')])
        self.store.put('recent_archive_cursor','005930',{'status':'PROVIDER_LIMIT','end':'2026-09-19'},observed_at=AS_OF)
        self.store.put('material_archive_cursor','005930',{'status':'PROVIDER_LIMIT','end':'2026-09-19'},observed_at=AS_OF)
        report=self.run_cycle()
        self.assertEqual(report['refresh_stages']['archive']['symbol'],'000001')
        self.assertEqual(self.news.archive_batch.call_count,1)

    def test_capped_issuer_material_search_cannot_monopolize_the_broad_archive_budget(self):
        self.repo.save_securities([replace(samsung(), symbol='000001')])
        self.store.put('recent_archive_cursor', '005930', {'status': 'PROVIDER_LIMIT', 'end': '2026-09-19'}, observed_at=AS_OF)
        state = completed_risk_state()
        state['status'] = 'PARTIAL'
        for q in state['queries']:q['status'] = 'PARTIAL'
        self.store.put('risk_archive_cursor', '005930', state, observed_at=AS_OF)
        with patch('stock_assistant.unified_cycle.review_official_contracts', return_value={'verified': [], 'pending': []}):
            report = refresh_unified(self.reader, self.store, dart_client=FakeDart(), news_client=self.news,
                calendar_client=self.calendar, dart_key='fixture-only', archive_pages=5, clock=lambda: AS_OF)
        self.assertEqual(report['refresh_stages']['archive']['symbol'], '000001')
        self.assertEqual(report['refresh_stages']['material_archive']['symbol'], '005930')
        self.assertEqual(report['refresh_stages']['material_archive']['actual_calls'], 4)
        self.assertEqual(report['refresh_stages']['news_budget'], {'reserved_calls': 5, 'actual_calls': 5})
        self.news.archive_batch.assert_called_once_with(5)
        self.assertEqual(self.news.archive_page.call_count, 5)
        # Risk-only supplementation never certifies the missing broad history.
        self.assertIsNone(self.store.latest('news_coverage', '005930', as_of=AS_OF))

    def test_material_failure_preserves_successful_broad_archive_and_original_call_limit(self):
        self.repo.save_securities([replace(samsung(), symbol='000001')])
        self.store.put('recent_archive_cursor', '005930', {'status': 'PROVIDER_LIMIT', 'end': '2026-09-19'}, observed_at=AS_OF)
        self.news.archive_page.side_effect = [NewsPage((), 0, 1, 0, AS_OF), ValueError('invalid material page')]
        with patch('stock_assistant.unified_cycle.review_official_contracts', return_value={'verified': [], 'pending': []}):
            report = refresh_unified(self.reader, self.store, dart_client=FakeDart(), news_client=self.news,
                calendar_client=self.calendar, dart_key='fixture-only', archive_pages=5, clock=lambda: AS_OF)
        self.assertEqual(report['refresh_stages']['archive']['status'], 'COMPLETE')
        self.assertEqual(report['refresh_stages']['material_archive']['status'], 'UNAVAILABLE')
        self.assertEqual(report['refresh_stages']['news_budget']['actual_calls'], 2)
        self.news.archive_batch.assert_called_once_with(5)

    def test_rate_limit_halts_material_requests_within_the_shared_reservation(self):
        self.repo.save_securities([replace(samsung(), symbol='000001')])
        self.store.put('recent_archive_cursor', '005930', {'status': 'PROVIDER_LIMIT', 'end': '2026-09-19'}, observed_at=AS_OF)
        self.news.archive_page.side_effect = RateLimitError('fixture provider limit')
        with patch('stock_assistant.unified_cycle.review_official_contracts', return_value={'verified': [], 'pending': []}):
            report = refresh_unified(self.reader, self.store, dart_client=FakeDart(), news_client=self.news,
                calendar_client=self.calendar, dart_key='fixture-only', archive_pages=5, clock=lambda: AS_OF)
        self.assertEqual(report['refresh_stages']['archive']['status'], 'UNAVAILABLE')
        self.assertEqual(report['refresh_stages']['material_archive']['status'], 'UNAVAILABLE')
        self.assertEqual(report['refresh_stages']['news_budget']['actual_calls'], 1)
        self.news.archive_batch.assert_called_once_with(5)
        self.assertEqual(self.news.archive_page.call_count, 1)

    def test_stale_ready_check_is_rendered_as_pending_with_required_session(self):
        self.repo.save_daily_check({'status': 'READY', 'checked_at': AS_OF.isoformat(), 'target_date': '2026-09-18'})
        report = self.run_cycle()
        self.assertEqual(report['refresh_stages']['prices']['status'], 'VERIFY_PENDING')
        self.assertEqual(report['refresh_stages']['prices']['required_session'], '2026-09-19')
        self.assertIn('가격 자료: 확인 대기 · 필요한 일봉 2026-09-19', render_recommendations(report))
        self.assertEqual(report['candidates'], [])

