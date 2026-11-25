from datetime import datetime, timedelta, timezone
import json
from subprocess import CompletedProcess
import unittest

from stock_assistant.swing_job import run_daily


def at(hour, minute, second=0):
    return datetime(2026, 11, 25, hour, minute, second, tzinfo=timezone(timedelta(hours=9)))


def result(status=None, reason=None, *, error_class=None, provider='KRX_OPEN_API', cause='TimeoutError'):
    error = {'stage': 'DAILY_REFRESH', 'status': 'FAILED', 'error_class': error_class,
             'request': {'provider': provider, 'cause_class': cause}}
    report = {'status': status, 'reason': reason}
    return CompletedProcess([], int(error_class is not None), json.dumps(report) if status else '',
                            json.dumps(error) if error_class else '')


class SwingRetryTests(unittest.TestCase):
    def run_window(self, outcomes, *, start=at(8, 35), once=False):
        now = start
        commands = []
        pending = iter(outcomes)

        def attempt(stage, cutoff):
            nonlocal now
            commands.append((stage, now, cutoff))
            outcome = next(pending)
            if isinstance(outcome, tuple):
                outcome, now = outcome
            return outcome

        def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        return run_daily(attempt, once=once, clock=lambda: now, sleep=sleep), commands

    def test_retries_krx_timeout_or_unpublished_data_at_0848(self):
        for error_class in ('NetworkError', 'KrxDailyDataUnavailable'):
            with self.subTest(error_class=error_class):
                ready = result('READY')
                final, commands = self.run_window([result(error_class=error_class), result(), ready])
                self.assertIs(final, ready)
                self.assertEqual(commands, [
                    ('daily-refresh', at(8, 35), at(8, 55)),
                    ('daily-refresh', at(8, 48), at(8, 55)),
                    ('refresh-swing', at(8, 48), at(8, 55)),
                ])

    def test_retries_unverified_report_once_and_preserves_terminal_pending(self):
        pending = result('VERIFY_PENDING', 'SWING_KRX_DAILY_UNVERIFIED')
        final, commands = self.run_window([result(), pending, result(), pending])
        self.assertIs(final, pending)
        self.assertEqual(commands, [
            ('daily-refresh', at(8, 35), at(8, 55)), ('refresh-swing', at(8, 35), at(8, 55)),
            ('daily-refresh', at(8, 48), at(8, 55)), ('refresh-swing', at(8, 48), at(8, 55)),
        ])

    def test_cutoff_prevents_new_stages_and_discards_late_ready_report(self):
        for outcomes, count in (([(result(), at(8, 55))], 1),
                                ([result(), (result('READY'), at(8, 55))], 2)):
            with self.subTest(count=count):
                final, commands = self.run_window(outcomes)
                self.assertEqual(len(commands), count)
                self.assertEqual((final.returncode, final.stdout), (124, ''))

    def test_no_wait_for_non_krx_errors_or_manual_once(self):
        for initial, time, once, cutoff in (
            (result(error_class='AuthenticationError'), at(8, 35), False, at(8, 55)),
            (result(error_class='NetworkError', provider='TOSS_SECURITIES_OPEN_API'), at(8, 35), False, at(8, 55)),
            (result(error_class='KrxDailyDataUnavailable'), at(8, 35), True, None),
        ):
            with self.subTest(error=initial.stderr, time=time, once=once):
                final, commands = self.run_window([initial], start=time, once=once)
                self.assertIs(final, initial)
                self.assertEqual(commands, [('daily-refresh', time, cutoff)])

    def test_outside_window_skips_without_attempting_refresh(self):
        for start in (at(8, 29, 59), at(8, 55)):
            with self.subTest(start=start):
                final, commands = self.run_window([], start=start)
                self.assertFalse(commands)
                self.assertEqual(final.returncode, 0)
                self.assertEqual(json.loads(final.stdout)['reason'], 'SWING_OUTSIDE_PREMARKET_WINDOW')

    def test_skipped_and_other_pending_reports_are_terminal(self):
        for report in (result('SKIPPED', 'SWING_NON_TRADING_DAY'),
                       result('VERIFY_PENDING', 'SWING_KRX_HISTORY_INCOMPLETE')):
            with self.subTest(report=report.stdout):
                final, commands = self.run_window([result(), report])
                self.assertIs(final, report)
                self.assertEqual([time for _, time, _ in commands], [at(8, 35)] * 2)
