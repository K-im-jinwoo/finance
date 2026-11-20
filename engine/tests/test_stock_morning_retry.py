from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from subprocess import CompletedProcess
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "jobs"))
from run_stock_morning import run_morning


KST = timezone(timedelta(hours=9))


def at(hour, minute, second=0):
    return datetime(2026, 11, 20, hour, minute, second, tzinfo=KST)


def failure(error_class, *, cause=None, stage="DAILY_REFRESH", code=1):
    diagnostic = {"stage": stage, "status": "FAILED", "error_class": error_class}
    if cause:
        diagnostic["request"] = {"cause_class": cause}
    error = f"{json.dumps(diagnostic)}\nSTOCK_CYCLE status=FAILED stage={stage} exit={code}\n"
    return CompletedProcess([], code, "", error)


class MorningRetryTests(unittest.TestCase):
    def run_window(self, results, *, starts=at(8, 10, 45)):
        now = starts
        calls = []
        outcomes = iter(results)

        def sleep(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)

        def attempt(cutoff):
            calls.append((now, cutoff))
            return next(outcomes)

        result = run_morning(attempt, clock=lambda: now, sleep=sleep)
        self.assertTrue(all(cutoff == at(8, 50) for _, cutoff in calls))
        return result, [started for started, _ in calls], now

    def test_retries_only_the_daily_step_then_returns_one_report(self):
        report = CompletedProcess([], 0, "오늘 보고서\n", "")
        result, started, _ = self.run_window([
            failure("NetworkError", cause="TimeoutError"),
            failure("KrxDailyDataUnavailable"),
            report,
        ])
        self.assertIs(result, report)
        self.assertEqual(started, [at(8, 10, 45), at(8, 25), at(8, 45)])

    def test_missing_data_waits_until_the_cutoff_for_one_final_failure(self):
        result, started, ended = self.run_window([failure("KrxDailyDataUnavailable")] * 3)
        self.assertEqual(started, [at(8, 10, 45), at(8, 25), at(8, 45)])
        self.assertEqual(ended, at(8, 50))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("08:50", result.stderr)

    def test_terminal_errors_bypass_retries(self):
        for initial in (
            failure("AuthenticationError"),
            failure("NetworkError", cause="ConnectionResetError"),
            failure("NetworkError", cause="TimeoutError", stage="UNIFIED_RESEARCH"),
        ):
            with self.subTest(error=initial.stderr):
                result, started, _ = self.run_window([initial])
                self.assertIs(result, initial)
                self.assertEqual(started, [at(8, 10, 45)])

    def test_late_initial_dispatch_uses_only_remaining_slots(self):
        report = CompletedProcess([], 0, "오늘 보고서\n", "")
        result, started, _ = self.run_window(
            [failure("KrxDailyDataUnavailable"), report], starts=at(8, 30)
        )
        self.assertIs(result, report)
        self.assertEqual(started, [at(8, 30), at(8, 45)])

    def test_expired_window_cannot_start_a_report(self):
        result, started, ended = self.run_window([], starts=at(8, 50))
        self.assertEqual(started, [])
        self.assertEqual(ended, at(8, 50))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
