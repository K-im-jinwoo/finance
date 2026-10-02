from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stock_assistant.approvals import ApprovalError, ApprovalStore


UTC = timezone.utc


class ApprovalTests(unittest.TestCase):
    def test_approval_is_bound_to_user_purpose_payload_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime(2026, 9, 21, tzinfo=UTC)
            store = ApprovalStore(
                Path(directory) / "approval.sqlite3", now=lambda: now,
                token_factory=lambda: "J-123456789ABC",
            )
            payload = {"relative_path": "wiki/20_Areas/Investments/x.md", "hash": "abc"}
            issued = store.issue(purpose="wiki_journal", user_id="42", payload=payload)
            self.assertEqual(issued.approval_id, "J-123456789ABC")
            with self.assertRaisesRegex(ApprovalError, "scope mismatch"):
                store.consume(issued.approval_id, purpose="wiki_journal", user_id="99", payload=payload)
            with self.assertRaisesRegex(ApprovalError, "changed"):
                store.consume(issued.approval_id, purpose="wiki_journal", user_id="42", payload={"hash": "changed"})
            store.consume(issued.approval_id, purpose="wiki_journal", user_id="42", payload=payload)
            with self.assertRaisesRegex(ApprovalError, "already consumed"):
                store.consume(issued.approval_id, purpose="wiki_journal", user_id="42", payload=payload)

    def test_expired_approval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = [datetime(2026, 9, 21, tzinfo=UTC)]
            store = ApprovalStore(
                Path(directory) / "approval.sqlite3", ttl_seconds=1,
                now=lambda: clock[0], token_factory=lambda: "J-EXPIRED00001",
            )
            payload = {"a": 1}
            pending = store.issue(purpose="wiki_journal", user_id="42", payload=payload)
            clock[0] += timedelta(seconds=2)
            with self.assertRaisesRegex(ApprovalError, "expired"):
                store.consume(pending.approval_id, purpose="wiki_journal", user_id="42", payload=payload)


if __name__ == "__main__":
    unittest.main()

