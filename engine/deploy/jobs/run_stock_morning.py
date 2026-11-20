"""Run the existing morning stock cycle with a bounded KRX refresh window."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


KST = timezone(timedelta(hours=9))
RETRY_MINUTES = (25, 45)


def kst_now() -> datetime:
    return datetime.now(KST)


def retryable_daily_failure(result: subprocess.CompletedProcess[str], *, cutoff_reached: bool) -> bool:
    if not result.returncode:
        return False
    lines = result.stderr.splitlines()
    if not any(line.startswith("STOCK_CYCLE status=FAILED stage=DAILY_REFRESH exit=") for line in lines):
        return False
    if result.returncode == 124 or (cutoff_reached and result.returncode == 137):
        return True
    for line in reversed(lines):
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (not isinstance(event, dict) or event.get("stage") != "DAILY_REFRESH"
                or event.get("status") != "FAILED" or "event" in event):
            continue
        if event.get("error_class") == "KrxDailyDataUnavailable":
            return True
        request = event.get("request")
        return (event.get("error_class") == "NetworkError" and isinstance(request, dict)
                and request.get("cause_class") == "TimeoutError")
    return False


def run_morning(attempt, *, clock=kst_now, sleep=time.sleep) -> subprocess.CompletedProcess[str]:
    started = clock()
    hour_start = started.replace(hour=8, minute=0, second=0, microsecond=0)
    cutoff = hour_start + timedelta(minutes=50)
    slots = [max(started, hour_start + timedelta(minutes=10))]
    slots.extend(hour_start + timedelta(minutes=minute) for minute in RETRY_MINUTES
                 if hour_start + timedelta(minutes=minute) > started)

    def wait_until(slot: datetime) -> None:
        while (remaining := (slot - clock()).total_seconds()) > 0:
            sleep(remaining)

    for index, slot in enumerate(slots):
        if index and clock() > slot:
            continue
        wait_until(slot)
        if clock() >= cutoff:
            break
        result = attempt(cutoff)
        if not retryable_daily_failure(result, cutoff_reached=clock() >= cutoff):
            return result

    wait_until(cutoff)
    return subprocess.CompletedProcess(
        [], 1, "",
        "08:50 KST까지 직전 거래일 일봉 확인을 완료하지 못했습니다. 아침 보고서 생성을 중단했습니다.\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one strict refresh and report outside the schedule")
    args = parser.parse_args()
    cycle = Path(__file__).with_name("run_stock_cycle.sh")

    def attempt(cutoff: datetime) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(cycle), "morning-window", str(int(cutoff.timestamp()))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    result = attempt(kst_now() + timedelta(minutes=5)) if args.once else run_morning(attempt)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
