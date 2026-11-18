"""Offline split-window coverage and recommendation regressions."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from stock_assistant.dart_research import DartResearchCollector
from stock_assistant.financial_research import evaluate_financial
from stock_assistant.material_archive import collect_material_archive
from stock_assistant.news_archive import collect_archive
from stock_assistant.news_policy import news_windows, RISK_QUERY_PLAN, RISK_QUERY_VERSION
from stock_assistant.news_research import score_news
from stock_assistant.providers.news import NewsPage
from stock_assistant.research_data import ResearchData
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.research_workflow import import_reviews
from stock_assistant.repository import StockRepository
from stock_assistant.risk_history import risk_coverage_payload
from stock_assistant.research_focus import choose_focus
from stock_assistant.unified_cycle import refresh_unified
from stock_assistant.unified_research import evaluate_unified
from tests.test_dart_research import FakeDart
from tests.test_screening import AS_OF, samsung, good_financial
from tests.test_unified_research import event, coverage
from tests.test_financial_research import history
from tests.helpers import make_bars


def recent_coverage(now=AS_OF):
    windows = news_windows(now)
    return {'symbol': '005930', 'start': windows['recent_start'].isoformat(), 'end': windows['end'].isoformat(),
            'observed_at': now.isoformat(), 'reviewed_at': now.isoformat(), 'status': 'AUTO_SEARCH_VERIFIED',
            'scope': 'NAVER_RECENT_NEWS_QUERY_RESULTS', 'sources': ['RECENT_NEWS_ARCHIVE'],
            'source_refs': ['recent-fixture'], 'reviewer': 'fixture-reviewer'}


def risk_coverage(now=AS_OF):
    return {**recent_coverage(now), 'start': news_windows(now)['risk_start'].isoformat(),
            'scope': 'NAVER_MATERIAL_QUERIES_AND_DART', 'query_version': RISK_QUERY_VERSION,
            'sources': ['RISK_SEARCH', 'DART_HISTORY'], 'source_refs': ['risk-fixture', 'dart-fixture']}


def completed_risk_state(now=AS_OF):
    windows = news_windows(now)
    return {'start': windows['risk_start'].isoformat(), 'end': windows['end'].isoformat(),
            'status': 'COMPLETE', 'query_version': RISK_QUERY_VERSION,
            'queries': [{'keyword': word, 'category': category, 'status': 'COMPLETE', 'next_start': 1,
                         'anchor_url': None} for word, category in RISK_QUERY_PLAN]}


def financial_review():
    return evaluate_financial(samsung(), good_financial(), as_of=AS_OF, annual_history=history())


class SplitScoreTests(unittest.TestCase):
    def score(self, events=(), recent=None, risk=None, **kwargs):
        return score_news('005930', events, recent or recent_coverage(), as_of=AS_OF,
                          risk_coverage=risk, **kwargs)

    def test_recent_score_is_available_while_missing_risk_history_still_blocks_buy(self):
        news = self.score()
        self.assertEqual(news['news_score'], 50)
        self.assertEqual(news['pending_checks'], ('RISK_HISTORY_COVERAGE_UNAVAILABLE',))
        financial = financial_review()
        verdict = evaluate_unified(financial, news, {'status': 'READY', 'reasons': []}, provenance_current=True)
        self.assertEqual(verdict['total_score'], 85)
        self.assertEqual(verdict['action_decision'], 'VERIFY_PENDING')

    def test_both_windows_allow_original_thresholds_and_price_gate_to_decide(self):
        news = self.score(risk=risk_coverage())
        financial = financial_review()
        for price, expected in [({'status': 'READY', 'reasons': []}, 'BUY_RECOMMENDATION'),
                                ({'status': 'WATCH', 'reasons': ['PRICE_SETUP_UNAVAILABLE']}, 'WATCH'),
                                ({'status': 'VERIFY_PENDING', 'reasons': ['PRICE_REFRESH_UNVERIFIED']}, 'VERIFY_PENDING')]:
            result = evaluate_unified(financial, news, price, provenance_current=True)
            self.assertEqual(result['total_score'], 85)
            self.assertEqual(result['action_decision'], expected)
        below = evaluate_unified(replace(financial, financial_score=69), news,
                                 {'status': 'READY', 'reasons': []}, provenance_current=True)
        self.assertEqual(below['action_decision'], 'WATCH')

    def test_old_active_negative_blocks_even_without_hard_exclusion_and_high_scores(self):
        old = event(direction='NEGATIVE', category='DILUTION', materiality='LOW', valid_until=None,
                    announced_at=AS_OF-timedelta(days=1500))
        news = self.score([old], risk=risk_coverage())
        result = evaluate_unified(financial_review(), news,
                                  {'status': 'READY', 'reasons': []}, provenance_current=True)
        self.assertEqual(result['total_score'], 80)
        self.assertEqual(result['action_decision'], 'VERIFY_PENDING')
        self.assertIn('UNRESOLVED_NEGATIVE_EVENT:CONTRACT-1', result['pending_checks'])
        self.assertEqual(self.score([replace(old, state='RESOLVED')], risk=risk_coverage())['pending_checks'], ())

    def test_positive_ninety_day_boundary_and_old_valid_contract(self):
        start = news_windows(AS_OF)['recent_start']
        at_start = datetime.combine(start, datetime.min.time(), tzinfo=timezone(timedelta(hours=9)))
        inside = event(announced_at=at_start)
        self.assertEqual(self.score([inside], risk=risk_coverage())['news_score'], 70)
        outside = replace(inside, announced_at=at_start-timedelta(seconds=1))
        result = self.score([outside], risk=risk_coverage())
        self.assertEqual(result['news_score'], 50)
        self.assertEqual(result['events'][0]['status'], 'OUTSIDE_RECENT_WINDOW')

    def test_short_recent_window_and_capped_risk_are_not_certified(self):
        short = {**recent_coverage(), 'start': (news_windows(AS_OF)['recent_start']+timedelta(days=1)).isoformat()}
        self.assertIsNone(self.score(recent=short, risk=risk_coverage())['news_score'])
        limited = {**risk_coverage(), 'status': 'PROVIDER_LIMIT'}
        self.assertFalse(self.score(risk=limited)['risk_coverage_complete'])
        self.assertIn('RISK_HISTORY_COVERAGE_UNAVAILABLE', self.score(risk=limited)['pending_checks'])

    def test_wrong_issuer_future_risk_and_stale_review_are_rejected_or_pending(self):
        with self.assertRaises(ValueError):self.score(risk={**risk_coverage(), 'symbol': '000001'})
        with self.assertRaises(ValueError):
            self.score(risk={**risk_coverage(), 'reviewed_at': (AS_OF+timedelta(seconds=1)).isoformat()})
        stale = {**risk_coverage(), 'observed_at': (AS_OF-timedelta(days=8)).isoformat(),
                 'reviewed_at': (AS_OF-timedelta(days=8)).isoformat()}
        self.assertFalse(self.score(risk=stale)['risk_coverage_complete'])
        future_end = (AS_OF.astimezone(timezone(timedelta(hours=9))).date()+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):self.score(risk={**risk_coverage(), 'end': future_end})

    def test_legacy_automatic_broad_search_does_not_substitute_for_targeted_risk(self):
        legacy = {**coverage(), 'status': 'AUTO_SEARCH_VERIFIED', 'scope': 'NAVER_INDEXED_QUERY_RESULTS'}
        result = score_news('005930', [], legacy, as_of=AS_OF)
        self.assertEqual(result['news_score'], 50)
        self.assertFalse(result['risk_coverage_complete'])

    def test_windows_are_inclusive_korean_dates_and_leap_safe(self):
        windows = news_windows(AS_OF)
        self.assertEqual((windows['end']-windows['recent_start']).days+1, 90)
        self.assertEqual(news_windows(datetime(2028, 3, 1, tzinfo=timezone(timedelta(hours=9))))['risk_start'], date(2025, 2, 28))


class SplitCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)/'source.sqlite3'
        repo = StockRepository(self.source);repo.save_securities([samsung()])
        repo.save_bars(make_bars(start=date(2026, 7, 2)))
        repo.save_daily_check({'status': 'READY', 'checked_at': AS_OF.isoformat(), 'target_date': '2026-09-19'})
        self.repo = ReadOnlyResearchRepository(self.source)
        self.store = ResearchData(Path(self.tmp.name)/'research.sqlite3', source_database=self.source)
        DartResearchCollector(self.repo, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None).run()
        self.news = Mock();self.news.archive_page.return_value = NewsPage((), 0, 1, 0, AS_OF)

    def test_recent_collection_has_separate_lineage_and_ninety_day_window(self):
        collect_archive(self.repo, self.store, self.news, pages=1, clock=lambda: AS_OF, recent=True)
        payload = self.store.latest('recent_news_coverage', '005930', as_of=AS_OF)['payload']
        self.assertEqual(payload['start'], '2026-06-22')
        self.assertEqual(payload['end'], '2026-09-19')
        self.assertIsNone(self.store.latest('news_coverage', '005930', as_of=AS_OF))
        self.assertIsNone(self.store.latest('archive_cursor', '005930', as_of=AS_OF))
        self.assertEqual(self.store.get(payload['source_refs'][0], as_of=AS_OF)['kind'], 'recent_archive_cursor')

    def test_risk_queries_resume_same_issuer_and_all_ten_are_required(self):
        first = collect_material_archive(self.repo, self.store, self.news, pages=5, clock=lambda: AS_OF, split_policy=True)
        self.assertEqual(first['status'], 'PARTIAL')
        self.assertIsNone(risk_coverage_payload(self.store, '005930', as_of=AS_OF))
        second = collect_material_archive(self.repo, self.store, self.news, pages=5,
                                          clock=lambda: AS_OF+timedelta(hours=1), split_policy=True)
        self.assertEqual(second['status'], 'COMPLETE')
        payload = risk_coverage_payload(self.store, '005930', as_of=AS_OF+timedelta(hours=1))
        self.assertIsNotNone(payload)
        self.assertEqual(payload['sources'], ['RISK_SEARCH', 'DART_HISTORY'])
        self.assertEqual(self.news.archive_page.call_count, 10)

    def test_completed_history_is_not_researched_daily_and_recent_archive_bridges_date_change(self):
        self.store.put('risk_archive_cursor', '005930', completed_risk_state(), observed_at=AS_OF)
        tomorrow = AS_OF+timedelta(days=1)
        self.assertIsNone(risk_coverage_payload(self.store, '005930', as_of=tomorrow))
        DartResearchCollector(self.repo, self.store, FakeDart(), clock=lambda: tomorrow, sleeper=lambda _: None).run()
        collect_archive(self.repo, self.store, self.news, pages=1, clock=lambda: tomorrow, recent=True)
        result = collect_material_archive(self.repo, self.store, self.news, pages=5, clock=lambda: tomorrow, split_policy=True)
        self.assertEqual(result['status'], 'CURRENT')
        payload = risk_coverage_payload(self.store, '005930', as_of=tomorrow)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['end'], '2026-09-20')
        self.assertIn('RECENT_NEWS_ARCHIVE', payload['sources'])
        self.assertEqual(self.news.archive_page.call_count, 1)

    def test_partial_gap_or_one_capped_query_cannot_create_risk_coverage(self):
        state = completed_risk_state()
        state['queries'][0]['status'] = 'PROVIDER_LIMIT'
        self.store.put('risk_archive_cursor', '005930', state, observed_at=AS_OF)
        self.assertIsNone(risk_coverage_payload(self.store, '005930', as_of=AS_OF))
        state = completed_risk_state()
        state['end'] = '2026-06-20'
        self.store.put('risk_archive_cursor', '005930', state, observed_at=AS_OF)
        collect_archive(self.repo, self.store, self.news, pages=1, clock=lambda: AS_OF, recent=True)
        self.assertIsNone(risk_coverage_payload(self.store, '005930', as_of=AS_OF))

    def test_forged_risk_review_has_no_sources_and_import_is_atomic(self):
        with self.assertRaises(ValueError):
            import_reviews(self.store, {'risk_coverage': [{**risk_coverage(), 'status': 'REVIEWED'}]},
                           as_of=AS_OF, clock=lambda: AS_OF)
        self.assertEqual(self.store.records('risk_coverage', '005930', as_of=AS_OF), [])

    def test_completed_search_round_rotates_to_unstarted_issuer_without_changing_verdict(self):
        repo = StockRepository(self.source)
        bundle = self.store.latest('financial_bundle', '005930', as_of=AS_OF)['payload']
        for i in range(1, 12):
            symbol = f'{i:06d}'
            repo.save_securities([replace(samsung(), symbol=symbol)])
            self.store.put('financial_bundle', symbol, json.loads(json.dumps(bundle).replace('005930', symbol)), observed_at=AS_OF)
        self.store.put('search_round_completed', '000001', {'status': 'SEARCH_COMPLETE', 'review_pending': True}, observed_at=AS_OF)
        self.assertEqual(choose_focus(self.repo, self.store, as_of=AS_OF), [f'{i:06d}' for i in range(2, 12)])

    def test_operating_cycle_finishes_both_windows_without_exceeding_five_calls(self):
        calendar = Mock();calendar.calendar.return_value = {
            'today': {'date': '2026-09-20', 'integrated': {}},
            'previousBusinessDay': {'date': '2026-09-19', 'integrated': {'regularMarket': True}}}
        reports = []
        for hour in range(3):
            now = AS_OF+timedelta(hours=hour)
            with patch('stock_assistant.unified_cycle.review_official_contracts', return_value={'verified': [], 'pending': []}):
                reports.append(refresh_unified(self.repo, self.store, dart_client=FakeDart(), news_client=self.news,
                    calendar_client=calendar, dart_key='fixture', archive_pages=5, clock=lambda: now))
            self.assertLessEqual(reports[-1]['refresh_stages']['news_budget']['actual_calls'], 5)
        self.assertEqual(reports[0]['evaluations'][0]['news_score'], '50')
        self.assertEqual(reports[0]['candidates'], [])
        self.assertEqual(reports[-1]['candidates'][0]['action_decision'], 'BUY_RECOMMENDATION')
        self.assertEqual(reports[-1]['candidates'][0]['total_score'], '85.0')
        self.assertFalse(reports[-1]['orders_enabled'])
        self.assertFalse(reports[-1]['paper_account_eligible'])


if __name__ == '__main__':unittest.main()
