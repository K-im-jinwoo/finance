from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone


KST = timezone(timedelta(hours=9), "Asia/Seoul")


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    job_id: str
    weekdays: tuple[int, ...]
    at: time
    description: str


SCHEDULES = (
    ScheduleSpec("morning-brief", (0, 1, 2, 3, 4), time(8, 30), "장 시작 전 후보 5개 심층 분석"),
    ScheduleSpec("evening-review", (0, 1, 2, 3, 4), time(20, 0), "장 마감 후 후보·보유종목 검토"),
    ScheduleSpec("weekly-review", (5,), time(12, 0), "주간 성과·오류·규칙 변경 검토"),
)


def due_jobs(now: datetime, *, window_minutes: int = 5) -> tuple[ScheduleSpec, ...]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if window_minutes < 0:
        raise ValueError("window_minutes cannot be negative")
    local = now.astimezone(KST)
    current_minutes = local.hour * 60 + local.minute
    due = []
    for schedule in SCHEDULES:
        scheduled_minutes = schedule.at.hour * 60 + schedule.at.minute
        if local.weekday() in schedule.weekdays and 0 <= current_minutes - scheduled_minutes < window_minutes:
            due.append(schedule)
    return tuple(due)
