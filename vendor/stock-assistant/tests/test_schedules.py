from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_assistant.schedules import due_jobs


UTC = timezone.utc


class ScheduleTests(unittest.TestCase):
    def test_weekday_morning_and_evening_jobs_use_kst(self) -> None:
        morning = due_jobs(datetime(2026, 9, 21, 23, 30, tzinfo=UTC))  # Tue 08:30 KST
        self.assertEqual([item.job_id for item in morning], ["morning-brief"])
        evening = due_jobs(datetime(2026, 9, 21, 11, 0, tzinfo=UTC))  # Mon 20:00 KST
        self.assertEqual([item.job_id for item in evening], ["evening-review"])

    def test_saturday_weekly_job_and_window_boundary(self) -> None:
        weekly = due_jobs(datetime(2026, 9, 26, 3, 0, tzinfo=UTC))
        self.assertEqual([item.job_id for item in weekly], ["weekly-review"])
        self.assertEqual(due_jobs(datetime(2026, 9, 26, 3, 5, tzinfo=UTC)), ())

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            due_jobs(datetime(2026, 9, 21, 8, 30))


if __name__ == "__main__":
    unittest.main()

