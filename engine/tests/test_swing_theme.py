from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from stock_assistant.models import Evidence, OHLCV
from stock_assistant.news_research import ReviewedEvent
from stock_assistant.providers.http import JsonResponse
from stock_assistant.providers.krx import KrxClient
from stock_assistant.performance import evaluate_all_report_performance, evaluate_report_performance, evaluate_swing_performance
from stock_assistant.providers.news import NewsArticle, NewsPage
from stock_assistant.providers.toss import TossMarketDataClient
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData
from stock_assistant.swing_market import managed_equity_codes, swing_price_signal
from stock_assistant.swing_theme import SWING_POLICY, refresh_swing, swing_catalysts, swing_report_id
from tests.test_screening import samsung


NOW = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)
DAYS = tuple(NOW.date() - timedelta(days=n) for n in range(20, -1, -1))


def bars(symbol, days=DAYS, *, close=125, at=NOW):
    return [OHLCV(symbol, day, Decimal('100'), Decimal(max(100, close)), Decimal('100'),
                  Decimal(close if index == len(days)-1 else 100), 100, 'KRX_OPEN_API', at)
            for index, day in enumerate(days)]


class SwingThemeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = StockRepository(Path(tmp.name) / 'stock.sqlite3')
        self.store = ResearchData(Path(tmp.name) / 'research.sqlite3', source_database=self.repo.database_path)

    def test_price_boundaries_use_real_turnover_and_strict_overheat_exclusion(self):
        values = [Decimal('1200000000')] * 20 + [Decimal('3000000000')]
        self.assertEqual(swing_price_signal(bars('005930'), values)['turnover_multiple'], Decimal('2.5'))
        self.assertIsNone(swing_price_signal(bars('005930', close=126), values))
        self.assertIsNone(swing_price_signal(bars('005930', close=100), values))
        self.assertIsNone(swing_price_signal(bars('005930'), values[:-1] + [values[-1] - 1]))

    def test_kind_management_snapshot_rejects_truncated_list(self):
        html = ('<table class="bbs_tb"><tr><th>종목명</th><th>종목코드</th></tr>'
                '<tr><td>예시기업</td><td>005930</td><td>2026-11-23</td></tr></table>')
        fetch = Mock()
        fetch.return_value.payload = html.encode()
        self.assertEqual(managed_equity_codes(fetch_bytes=fetch), {'005930'})
        fetch.return_value.payload = html.removesuffix('</table>').encode()
        with self.assertRaisesRegex(RuntimeError, 'KIND_MANAGEMENT_LIST_INCOMPLETE'):
            managed_equity_codes(fetch_bytes=fetch)

    def test_provider_contracts_preserve_actual_turnover_and_active_warnings(self):
        krx = KrxClient('fixture-key', fetch_json=Mock(return_value=JsonResponse(200, {'OutBlock_1': [{
            'BAS_DD': DAYS[-1].strftime('%Y%m%d'), 'ISU_CD': '005930', 'TDD_OPNPRC': '100',
            'ACC_TRDVOL': '123', 'ACC_TRDVAL': '3,001,000,000', 'MKTCAP': '50,000,000,000',
        }]})))
        self.assertEqual(krx.equity_values('KOSPI', DAYS[-1])['005930'],
                         {'turnover': '3001000000', 'market_cap': '50000000000'})
        fetch = Mock(side_effect=[JsonResponse(200, {'result': [{'symbol': '005930', 'status': 'ACTIVE'}]}),
                                  JsonResponse(200, {'result': [{'warningType': 'INVESTMENT_WARNING'}]})])
        market = TossMarketDataClient('fixture-id', 'fixture-secret', fetch_json=fetch,
                                      post_form=Mock(return_value=JsonResponse(200, {'access_token': 'fixture'})))
        self.assertIn('005930', market.stock_reference(['005930']))
        self.assertEqual(market.stock_warnings('005930'), {'INVESTMENT_WARNING'})
        self.assertTrue(fetch.call_args.args[0].endswith('/api/v1/stocks/005930/warnings'))

    def test_catalysts_require_a_company_event_and_point_in_time(self):
        security = samsung()
        def article(title, n, when=NOW):
            return NewsArticle(title, f'https://news.example/{n}', when, when)
        items = [article('삼성전자 신제품 출시', 1), article('삼성전자 수주 기대', 2),
                 article('삼성전자 테마주 강세', 3), article('삼성전자 반도체 수주', 4),
                 article('삼성전자 공급계약 체결', 5, NOW+timedelta(minutes=1)),
                 article('삼성전자 정부 정책 수혜주로 부각', 6),
                 article('삼성전자 신제품 출시 관련주 강세', 7),
                 article('삼성전자 정부 지원사업 선정', 8)]
        self.assertEqual({item['category'] for item in swing_catalysts(security, items, as_of=NOW)},
                         {'THEME', 'CONTRACT', 'POLICY'})
        self.assertNotIn('부각', ' '.join(item['title'] for item in swing_catalysts(security, items, as_of=NOW)))

    def test_refresh_filters_before_news_and_saves_one_session_report(self):
        rows = [replace(samsung(), symbol=f'{100000+i}', name=f'예시기업{i}') for i in range(16)]
        self.repo.save_securities(rows)
        self.repo.save_bars([bar for row in rows for bar in bars(row.symbol)])
        self.repo.save_daily_check({'status':'READY', 'target_date':DAYS[-1].isoformat(), 'checked_at':NOW.isoformat()})
        symbol = rows[4].symbol
        event = ReviewedEvent(symbol, 'dart-risk-fixture', 'MANAGEMENT', 'NEGATIVE', 'ACTIVE', 'HIGH', True, True,
                              NOW, NOW, None, 'fixture', 'official risk', (Evidence(
                                  'DART', '관리 위험 확인', 'https://dart.fss.or.kr/example',
                                  NOW, NOW, True, (f'ISSUER:{symbol}',)),))
        self.store.put('reviewed_event', symbol, event, observed_at=NOW)
        krx = Mock()
        krx.equity_values.side_effect = lambda market, day: {
            row.symbol: {'turnover': '3000000000' if day == DAYS[-1] else '1200000000',
                         'market_cap': '49999999999' if index == 1 else '50000000000'}
            for index, row in enumerate(rows) if market == row.market.value}
        market = Mock()
        market.calendar.return_value = {'today':{'date':DAYS[-1].isoformat(),
            'integrated':{'regularMarket':{'endTime':f'{DAYS[-1]}T15:30:00+09:00'}}},
            'previousBusinessDay':{'date':DAYS[-2].isoformat(),'integrated':{'regularMarket':True}}}
        market.stock_reference.return_value = {
            row.symbol: {'status':'ACTIVE', 'market':row.market.value,
                         'koreanMarketDetail':{'krxTradingSuspended': index == 2, 'liquidationTrading':False}}
            for index, row in enumerate(rows)}
        market.stock_warnings.side_effect = lambda symbol: {'INVESTMENT_WARNING'} if symbol == rows[3].symbol else set()
        news = Mock()
        news.archive_page.side_effect = lambda query, start: NewsPage((
            NewsArticle(f'{query} 공급계약 체결', f'https://news.example/{query}', NOW, NOW),), 1, 1, 1, NOW)
        report = refresh_swing(self.repo, self.store, krx_client=krx, market_client=market, news_client=news,
                               management_list=lambda: {rows[0].symbol},
                               clock=lambda:NOW, sleeper=lambda _:None)
        self.assertEqual(report['contract'], SWING_POLICY)
        self.assertEqual(report['report_id'], swing_report_id(DAYS[-1]))
        self.assertEqual([r['symbol'] for r in report['candidates']], [r.symbol for r in rows[5:8]])
        news.archive_batch.assert_called_once_with(11)
        self.assertEqual(news.archive_page.call_count, 11)
        again = refresh_swing(self.repo, self.store, krx_client=krx, market_client=market, news_client=news,
                              management_list=lambda: set(), clock=lambda:NOW, sleeper=lambda _:None)
        self.assertEqual(again, report)
        self.assertEqual(news.archive_page.call_count, 11)

    def test_weekly_swing_uses_only_3_5_10_and_does_not_skip_missing_entry(self):
        report_id = swing_report_id(DAYS[-1])
        unified_id = 'UR-' + 'a' * 24
        self.store.put('unified_report', 'ALL', {'report_id': unified_id, 'contract': 'UNIFIED_RESEARCH_V1',
            'policy_version': 'unified-fixture', 'as_of': NOW.isoformat(), 'candidates': [
                {'symbol': '005930', 'action_decision': 'BUY_RECOMMENDATION'}]}, observed_at=NOW)
        self.store.put('swing_report', DAYS[-1].isoformat(), {'report_id':report_id, 'contract':SWING_POLICY,
            'policy_version':SWING_POLICY, 'as_of':NOW.isoformat(), 'candidates':[
                {'symbol':symbol, 'action_decision':'SWING_CANDIDATE'}
                for symbol in ('005930', '000660', '035720')]}, observed_at=NOW)
        future = tuple(DAYS[-1]+timedelta(days=n) for n in range(1, 12))
        elapsed = NOW+timedelta(days=15)
        self.repo.save_bars(bars('005930', future, at=elapsed))
        self.repo.save_bars(bars('000660', future[1:], at=elapsed))
        halted = bars('035720', future, at=elapsed)
        self.repo.save_bars([replace(halted[0], volume=0), *halted[1:]])
        records = evaluate_report_performance(self.repo, report_id=report_id, evaluated_at=elapsed,
                                              research_database=self.store.path).records
        self.assertEqual(len(records), 9)
        entry = next(row for row in records if row.symbol == '005930' and row.trading_days == 10)
        self.assertEqual((entry.entry_date, entry.net_return), (future[0].isoformat(), Decimal('0.247')))
        self.assertEqual({r.trading_days for r in records}, {3, 5, 10})
        self.assertEqual({r.status for r in records if r.symbol == '000660'}, {'PENDING_ENTRY'})
        self.assertEqual({r.status for r in records if r.symbol == '035720'}, {'UNTRADABLE_ENTRY'})
        weekly = evaluate_swing_performance(self.repo, evaluated_at=elapsed, research_database=self.store.path)
        self.assertEqual([row['sample_count'] for row in weekly['summary']], [1, 1, 1])
        self.assertEqual(weekly['evaluated_reports'], 1)
        unified = evaluate_all_report_performance(self.repo, evaluated_at=elapsed, research_database=self.store.path)
        self.assertEqual([item.report_id for item in unified], [unified_id])
        self.assertEqual({row.trading_days for row in unified[0].records}, {5, 20, 60})
