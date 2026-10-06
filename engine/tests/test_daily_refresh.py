from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from stock_assistant.daily_refresh import DailyRefresh
from stock_assistant.ingestion import KrxHistoryIngestor
from stock_assistant.models import AssetType, CompanyKind, Market, OHLCV, Security
from stock_assistant.pipeline import CandidatePipeline
from stock_assistant.providers.http import AuthenticationError
from stock_assistant.providers.krx import KrxDailySnapshot
from stock_assistant.repository import StockRepository


UTC = timezone.utc
NOW = datetime(2026, 10, 6, 3, tzinfo=UTC)
SYMBOLS = {'KOSPI': '005930', 'KOSDAQ': '035900', 'ETF': '069500'}
HOLIDAYS = {date(2026, 9, 24), date(2026, 9, 25), date(2026, 10, 5)}


def bar(symbol, day, observed):
    price = Decimal('10000')
    return OHLCV(symbol, day, price, price, price, price, 100000, 'KRX_OPEN_API', observed)


def calendar(day):
    def session(d):
        opened = d.weekday() < 5 and d not in HOLIDAYS
        return {'date': d.isoformat(), 'integrated': {'regularMarket': {'startTime': d.isoformat()} if opened else None}}
    previous = day - timedelta(days=1)
    while not session(previous)['integrated']['regularMarket']:
        previous -= timedelta(days=1)
    return {'today': session(day), 'previousBusinessDay': session(previous)}


class CompleteKrx:
    def __init__(self):
        self.calls = []
        self.empty_market = None
        self.error_market = None
        self.wrong_date = False

    def daily_snapshot(self, market, business_date, *, observed_at):
        self.calls.append((market, business_date))
        if market == self.error_market:
            raise AuthenticationError('provider returned HTTP 401')
        if market == self.empty_market:
            return KrxDailySnapshot((), {})
        symbol = SYMBOLS[market]
        day = business_date - timedelta(days=1) if self.wrong_date else business_date
        return KrxDailySnapshot((bar(symbol, day, observed_at),), {symbol: symbol})

    def securities(self, market, day):
        return [security(market)]


def security(market):
    return Security(SYMBOLS[market], market, Market.KOSDAQ if market == 'KOSDAQ' else Market.KOSPI,
                    AssetType.ETF if market == 'ETF' else AssetType.COMMON,
                    CompanyKind.FUND if market == 'ETF' else CompanyKind.GENERAL, None)


class DailyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = StockRepository(Path(self.directory.name) / 'stock.sqlite3')
        self.repo.save_securities([security(m) for m in SYMBOLS])
        self.repo.save_bars([bar(s, date(2026, 9, 23), NOW - timedelta(days=13)) for s in SYMBOLS.values()])
        self.client = CompleteKrx()
        self.cal = Mock(side_effect=calendar)
        self.counter = 0

    def clock(self):
        self.counter += 1
        return NOW + timedelta(seconds=self.counter)

    def refresh(self, **kwargs):
        return DailyRefresh(self.repo, KrxHistoryIngestor(self.repo, self.client, request_interval_seconds=0),
                            self.cal, clock=self.clock).run(**kwargs)

    def test_catches_up_missed_sessions_and_skips_verified_holidays(self):
        result = self.refresh()
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['target_date'], '2026-10-02')
        self.assertEqual(result['collected_dates'], ['2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02'])
        self.assertEqual(result['bars_saved'], 15)
        self.assertFalse(any(day in HOLIDAYS or day.weekday() >= 5 for _, day in self.client.calls))
        self.assertEqual(set(result['market_dates'].values()), {'2026-10-02'})
        calls = len(self.client.calls)
        rerun = self.refresh()
        self.assertEqual(rerun['bars_saved'], 0)
        self.assertEqual(len(self.client.calls), calls)

    def test_empty_trading_market_fails_before_any_day_is_saved_and_blocks_report(self):
        self.client.empty_market = 'ETF'
        with self.assertRaisesRegex(ValueError, 'unavailable: ETF'):
            self.refresh()
        self.assertEqual(set(self.repo.daily_market_dates(as_of=self.clock()).values()), {'2026-09-23'})
        self.assertEqual(self.repo.latest_daily_check(as_of=self.clock())['status'], 'FAILED')
        with self.assertRaisesRegex(ValueError, 'DAILY_DATA_NOT_READY'):
            CandidatePipeline(self.repo).run(as_of=self.clock())
        self.assertEqual(self.repo.list_report_ids(), [])

    def test_authentication_failure_is_not_misclassified_as_holiday(self):
        self.client.error_market = 'KOSDAQ'
        with self.assertRaises(AuthenticationError):
            self.refresh()
        self.assertEqual(set(self.repo.daily_market_dates(as_of=self.clock()).values()), {'2026-09-23'})
        self.assertEqual(self.repo.latest_daily_check(as_of=self.clock())['status'], 'FAILED')

    def test_wrong_provider_date_is_rejected(self):
        self.client.wrong_date = True
        with self.assertRaisesRegex(ValueError, 'different trade date'):
            self.refresh()
        self.assertEqual(set(self.repo.daily_market_dates(as_of=self.clock()).values()), {'2026-09-23'})

    def test_unverified_calendar_and_oversized_gap_fail_closed(self):
        self.cal.side_effect = AuthenticationError('calendar denied')
        with self.assertRaises(AuthenticationError):
            self.refresh()
        self.assertEqual(self.client.calls, [])
        self.cal.side_effect = calendar
        with self.assertRaisesRegex(ValueError, 'bounded calendar window'):
            self.refresh(max_calendar_days=3)
        self.assertEqual(self.client.calls, [])

    def test_yesterdays_readiness_cannot_authorize_a_new_report(self):
        self.repo.save_daily_check({'checked_at': (NOW - timedelta(days=1)).isoformat(),
                                   'status': 'READY', 'target_date': '2026-09-23'})
        with self.assertRaisesRegex(ValueError, 'DAILY_DATA_NOT_READY'):
            CandidatePipeline(self.repo).run(as_of=NOW)

    def test_new_report_exposes_the_verified_daily_date(self):
        self.refresh()
        # Sufficient history is required independently of daily freshness.
        for symbol in SYMBOLS.values():
            self.repo.save_bars([bar(symbol, date(2026, 6, 1) + timedelta(days=i), NOW - timedelta(days=20))
                                 for i in range(80)])
        result = CandidatePipeline(self.repo).run(as_of=self.clock())
        self.assertTrue(any('일봉 기준일: 2026-10-02' in fact for fact in result.report.fact_summary))

    def test_catchup_does_not_make_new_observations_available_in_the_past(self):
        self.refresh()
        bars = self.repo.bars_for('005930', as_of=NOW-timedelta(days=5))
        self.assertEqual([b.trade_date for b in bars], [date(2026,9,23)])

    def test_stale_candidate_is_rejected_even_when_other_markets_are_current(self):
        self.refresh()
        extra=Security('000001','old candidate',Market.KOSPI,AssetType.COMMON,CompanyKind.GENERAL,None)
        self.repo.save_securities([extra])
        self.repo.save_bars([bar(extra.symbol,date(2026,6,1)+timedelta(days=i),NOW-timedelta(days=20)) for i in range(80)])
        with self.assertRaisesRegex(ValueError,'candidate 000001 lacks'):
            CandidatePipeline(self.repo).run(as_of=self.clock())
        self.assertEqual(self.repo.list_report_ids(),[])


if __name__ == '__main__':
    unittest.main()
