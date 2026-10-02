from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from stock_assistant.news_cost import FREE_STATEMENT, NewsApiBudget, NewsCostBlocked, require_free_policy
from stock_assistant.providers.http import JsonResponse, NetworkError, RateLimitError
from stock_assistant.providers.news import NaverNewsClient

NOW = datetime(2026, 10, 2, 3, tzinfo=timezone.utc)


class FreePolicyTests(unittest.TestCase):
    def test_both_current_official_statements_are_required(self):
        fetch = Mock(return_value='<article><p>' + FREE_STATEMENT + '</p></article>')
        require_free_policy(fetch)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(call.args[0].startswith('https://guide.ncloud-docs.com/') for call in fetch.call_args_list))

    def test_paid_changed_missing_script_only_or_unreachable_policy_blocks(self):
        cases = [Mock(side_effect=TimeoutError('private diagnostics')),
                 Mock(return_value='<p>유료 전환되었습니다.</p>'),
                 Mock(return_value='<script>' + FREE_STATEMENT + '</script>'),
                 Mock(side_effect=['<p>' + FREE_STATEMENT + '</p>', '<p>unknown</p>'])]
        for fetch in cases:
            with self.subTest(fetch=fetch), self.assertRaisesRegex(NewsCostBlocked, 'NEWS_FREE_POLICY_UNCONFIRMED'):
                require_free_policy(fetch)


class NewsBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'budget.sqlite3'
        self.free = Mock()
        self.guard = NewsApiBudget(self.path, free_check=self.free)

    def initialize(self, daily=0, monthly=0, at=NOW - timedelta(hours=2)):
        NewsApiBudget.initialize(self.path, at=at, used_today=daily, used_month=monthly)

    def counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT daily_used,monthly_used FROM news_api_budget').fetchone()

    def client(self, fetch):
        return NaverNewsClient('fixture-id', 'fixture-secret', fetch_json=fetch,
                               budget=self.guard, clock=lambda: NOW)

    def test_batch_reserved_before_first_call_with_article_count_not_charged(self):
        self.initialize(7, 7)
        def fetch(*args, **kwargs):
            self.assertEqual(self.counts(), (12, 12))
            return JsonResponse(200, {'items': []})
        fetch = Mock(side_effect=fetch)
        self.assertEqual(self.client(fetch).collect(observed_at=NOW), [])
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(self.free.call_count, 1)

    def test_daily_and_monthly_boundary_reserve_whole_batch_or_make_zero_requests(self):
        for daily, monthly in ((16, 16), (0, 496)):
            with self.subTest(daily=daily, monthly=monthly):
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / 'budget.sqlite3'
                    NewsApiBudget.initialize(path, at=NOW - timedelta(hours=2), used_today=daily, used_month=monthly)
                    guard = NewsApiBudget(path, free_check=self.free)
                    fetch = Mock()
                    client = NaverNewsClient('fixture', 'fixture', fetch_json=fetch, clock=lambda: NOW, budget=guard)
                    with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_COST_LIMIT_REACHED'):
                        client.collect(observed_at=NOW)
                    fetch.assert_not_called()
        self.free.assert_not_called()

    def test_exact_limit_is_allowed_and_next_call_is_blocked(self):
        self.initialize(15, 495)
        self.guard.reserve(5, at=NOW)
        self.assertEqual(self.counts(), (20, 500))
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_COST_LIMIT_REACHED'):
            self.guard.reserve(1, at=NOW + timedelta(hours=1))

    def test_failures_are_not_refunded_and_never_retried(self):
        self.initialize()
        fetch = Mock(side_effect=NetworkError('fixture failure'))
        with self.assertRaises(NetworkError):
            self.client(fetch).collect(observed_at=NOW)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.counts(), (5, 5))
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_REFRESH_COOLDOWN'):
            self.client(fetch).collect(observed_at=NOW)
        self.assertEqual(fetch.call_count, 1)

    def test_provider_quota_rejection_halts_later_attempts_for_month(self):
        self.initialize()
        fetch = Mock(side_effect=RateLimitError('429 fixture'))
        with self.assertRaises(RateLimitError):
            self.client(fetch).collect(observed_at=NOW)
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_PROVIDER_QUOTA_BLOCKED'):
            self.guard.reserve(5, at=NOW + timedelta(days=1))
        self.assertEqual(fetch.call_count, 1)

    def test_parallel_connections_cannot_both_reserve_remaining_budget(self):
        self.initialize(15, 495)
        def reserve(_):
            try:
                NewsApiBudget(self.path, free_check=lambda: None).reserve(5, at=NOW)
                return 'ALLOWED'
            except NewsCostBlocked as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(reserve, range(2)))
        self.assertEqual(values.count('ALLOWED'), 1)
        self.assertEqual(self.counts(), (20, 500))

    def test_kst_midnight_resets_daily_only_and_month_start_resets_both(self):
        last = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
        self.initialize(20, 200, last)
        self.guard.reserve(5, at=last + timedelta(hours=2))
        self.assertEqual(self.counts(), (5, 205))
        self.guard.reserve(5, at=datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        self.assertEqual(self.counts(), (5, 5))

    def test_clock_reversal_missing_corrupt_or_negative_usage_fails_closed(self):
        fetch = Mock()
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_USAGE_STATE_UNAVAILABLE'):
            self.client(fetch).collect(observed_at=NOW)
        self.assertFalse(self.path.exists())
        self.initialize()
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_USAGE_CLOCK_REVERSED'):
            self.guard.reserve(5, at=NOW - timedelta(hours=3))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE news_api_budget SET daily_used=-1')
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_USAGE_STATE_UNAVAILABLE'):
            self.client(fetch).collect(observed_at=NOW)
        fetch.assert_not_called()
        self.free.assert_not_called()

    def test_unknown_free_policy_blocks_before_reservation_or_upstream(self):
        self.initialize()
        self.free.side_effect = NewsCostBlocked('NEWS_FREE_POLICY_UNCONFIRMED')
        fetch = Mock()
        with self.assertRaisesRegex(NewsCostBlocked, 'NEWS_FREE_POLICY_UNCONFIRMED'):
            self.client(fetch).collect(observed_at=NOW)
        self.assertEqual(self.counts(), (0, 0))
        fetch.assert_not_called()

    def test_runtime_default_budget_cannot_bypass_guard_when_state_is_missing(self):
        with patch.dict('os.environ', {'STOCK_NEWS_USAGE_FILE': str(self.path)}):
            fetch = Mock()
            client = NaverNewsClient('fixture', 'fixture', fetch_json=fetch)
            with self.assertRaises(NewsCostBlocked):
                client.collect(observed_at=NOW)
            fetch.assert_not_called()

    def test_reinitialization_invalid_counts_and_naive_time_are_rejected(self):
        self.initialize()
        with self.assertRaises(ValueError):
            self.initialize()
        for calls, at in ((True, NOW), (0, NOW), (6, NOW), (5, NOW.replace(tzinfo=None))):
            with self.subTest(calls=calls, at=at), self.assertRaises(ValueError):
                self.guard.reserve(calls, at=at)

if __name__ == '__main__':
    unittest.main()
