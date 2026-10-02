from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_assistant.alerts import AlertStore, DeliveryStatus


UTC = timezone.utc


class AlertTests(unittest.TestCase):
    def test_duplicate_event_and_cooldown_are_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AlertStore(Path(directory) / "alerts.sqlite3")
            now = datetime(2026, 9, 21, 1, tzinfo=UTC)
            first = store.issue(
                event_key="007660-volume-20260921T1000", symbol="007660",
                alert_type="VOLUME_SURGE", observed_at=now, payload={"ratio": "3"},
            )
            self.assertIsNotNone(first)
            duplicate = store.issue(
                event_key="007660-volume-20260921T1000", symbol="007660",
                alert_type="VOLUME_SURGE", observed_at=now, payload={"ratio": "3"},
            )
            self.assertIsNone(duplicate)
            cooldown = store.issue(
                event_key="007660-volume-20260921T1010", symbol="007660",
                alert_type="VOLUME_SURGE", observed_at=now + timedelta(minutes=10), payload={"ratio": "4"},
            )
            self.assertIsNone(cooldown)
            later = store.issue(
                event_key="007660-volume-20260921T1100", symbol="007660",
                alert_type="VOLUME_SURGE", observed_at=now + timedelta(hours=1), payload={"ratio": "4"},
            )
            self.assertIsNotNone(later)

    def test_analysis_creation_and_delivery_status_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AlertStore(Path(directory) / "alerts.sqlite3")
            now = datetime(2026, 9, 21, 1, tzinfo=UTC)
            alert = store.issue(
                event_key="event-1", symbol="005930", alert_type="THESIS_BREAK",
                observed_at=now, payload={"reason": "low break"}, cooldown_seconds=0,
            )
            self.assertEqual(store.get(alert.alert_id).status, DeliveryStatus.PENDING)
            store.mark_failed(alert.alert_id, "TELEGRAM_TIMEOUT")
            failed = store.get(alert.alert_id)
            self.assertEqual(failed.status, DeliveryStatus.FAILED)
            self.assertEqual(failed.attempt_count, 1)
            store.mark_sent(alert.alert_id, now + timedelta(minutes=1))
            sent = store.get(alert.alert_id)
            self.assertEqual(sent.status, DeliveryStatus.SENT)
            self.assertEqual(sent.attempt_count, 2)
            self.assertIsNone(sent.last_error_code)


if __name__ == "__main__":
    unittest.main()

