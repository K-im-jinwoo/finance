"""Run the daily swing refresh with one KRX retry in the premarket window."""

import argparse
from datetime import datetime
import json
import os
import subprocess
import sys
from time import sleep as sleep_seconds

from .swing_theme import KST, SWING_POLICY


def kst_now():
    return datetime.now(KST)


def _json_object(text):
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _retryable_krx_failure(result):
    for line in reversed(result.stderr.splitlines()):
        event = _json_object(line)
        if event.get('stage') != 'DAILY_REFRESH' or event.get('status') != 'FAILED' or 'event' in event:
            continue
        if event.get('error_class') == 'KrxDailyDataUnavailable':
            return True
        request = event.get('request')
        return (event.get('error_class') == 'NetworkError' and isinstance(request, dict)
                and request.get('provider') == 'KRX_OPEN_API' and request.get('cause_class') == 'TimeoutError')
    return False


def _cutoff_result():
    return subprocess.CompletedProcess([], 124, '', 'STOCK_SWING status=PREMARKET_CUTOFF time=08:55_KST\n')


def run_daily(attempt, *, once=False, clock=kst_now, sleep=sleep_seconds):
    started = clock().astimezone(KST)
    window_start = started.replace(hour=8, minute=30, second=0, microsecond=0)
    cutoff = window_start.replace(minute=55)
    retry_at = window_start.replace(minute=48)
    if once:
        cutoff = None
    elif not window_start <= started < cutoff:
        report = {'contract': SWING_POLICY, 'status': 'SKIPPED', 'reason': 'SWING_OUTSIDE_PREMARKET_WINDOW',
                  'orders_enabled': False, 'candidates': []}
        return subprocess.CompletedProcess([], 0, json.dumps(report) + '\n', '')

    for index in range(2):
        refresh = attempt('daily-refresh', cutoff)
        if cutoff and clock() >= cutoff:
            return _cutoff_result()
        if refresh.returncode:
            result = refresh
            retryable = _retryable_krx_failure(refresh)
        else:
            result = attempt('refresh-swing', cutoff)
            if cutoff and clock() >= cutoff:
                return _cutoff_result()
            report = _json_object(result.stdout)
            retryable = (result.returncode == 0 and report.get('status') == 'VERIFY_PENDING'
                         and report.get('reason') == 'SWING_KRX_DAILY_UNVERIFIED')
        if index or not cutoff or not retryable or started >= retry_at:
            return result
        print('STOCK_SWING status=WAITING_KRX_RETRY time=08:48_KST', file=sys.stderr)
        while (remaining := (retry_at - clock()).total_seconds()) > 0:
            sleep(remaining)
        if clock() >= cutoff:
            return _cutoff_result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()

    def attempt(stage, cutoff):
        command = ['sh', args.job, stage]
        if cutoff:
            command.append(str(int(cutoff.timestamp())))
        try:
            result = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                    timeout=max(0, (cutoff - kst_now()).total_seconds()) if cutoff else None)
        except subprocess.TimeoutExpired:
            return _cutoff_result()
        sys.stderr.write(result.stderr)
        return result

    result = run_daily(attempt, once=args.once or os.getenv('STOCK_SWING_ONCE') == 'true')
    if result.returncode == 124 and result.stderr.startswith('STOCK_SWING status=PREMARKET_CUTOFF'):
        sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
