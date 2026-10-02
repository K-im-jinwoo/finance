from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from stock_assistant.approvals import ApprovalError, ApprovalStore
from stock_assistant.journal_writer import ApprovedJournalWriter, JournalConflictError
from stock_assistant.reports import JournalDraft, build_journal_draft


UTC = timezone.utc


def make_draft():
    return build_journal_draft(
        event_date=date(2026, 9, 21), symbol="007660", company_name="이수페타시스",
        event_slug="position-review", report_id="R-1",
        confirmed_facts=("평단 102000원",), decisions=("추가매수 보류",),
    )


class JournalWriterTests(unittest.TestCase):
    def test_write_requires_bound_one_time_approval_and_is_idempotent_by_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approvals = ApprovalStore(
                root / "state" / "approvals.sqlite3",
                now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
                token_factory=lambda: "J-111111111111",
            )
            writer = ApprovedJournalWriter(root, approvals)
            draft = make_draft()
            pending = approvals.issue(purpose="wiki_journal", user_id="42", payload=draft.payload)
            result = writer.write(draft, approval_id=pending.approval_id, user_id="42")
            self.assertEqual(result.status, "created")
            self.assertEqual(result.path.read_text(encoding="utf-8"), draft.markdown)
            with self.assertRaisesRegex(ApprovalError, "already consumed"):
                writer.write(draft, approval_id=pending.approval_id, user_id="42")

            approvals2 = ApprovalStore(
                root / "state" / "approvals2.sqlite3",
                now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
                token_factory=lambda: "J-222222222222",
            )
            pending2 = approvals2.issue(purpose="wiki_journal", user_id="42", payload=draft.payload)
            matched = ApprovedJournalWriter(root, approvals2).write(
                draft, approval_id=pending2.approval_id, user_id="42",
            )
            self.assertEqual(matched.status, "matched")

    def test_writer_rejects_path_escape_before_consuming_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approvals = ApprovalStore(
                root / "approvals.sqlite3",
                now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
                token_factory=lambda: "J-333333333333",
            )
            draft = make_draft()
            escaped = JournalDraft("../escape.md", draft.title, draft.markdown, draft.payload)
            pending = approvals.issue(purpose="wiki_journal", user_id="42", payload=draft.payload)
            with self.assertRaisesRegex(ValueError, "traverse"):
                ApprovedJournalWriter(root, approvals).write(
                    escaped, approval_id=pending.approval_id, user_id="42",
                )
            approvals.consume(pending.approval_id, purpose="wiki_journal", user_id="42", payload=draft.payload)

    def test_writer_does_not_overwrite_different_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approvals = ApprovalStore(
                root / "approvals.sqlite3",
                now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
                token_factory=lambda: "J-444444444444",
            )
            draft = make_draft()
            target = root / draft.relative_path
            target.parent.mkdir(parents=True)
            target.write_text("existing", encoding="utf-8")
            pending = approvals.issue(purpose="wiki_journal", user_id="42", payload=draft.payload)
            with self.assertRaises(JournalConflictError):
                ApprovedJournalWriter(root, approvals).write(
                    draft, approval_id=pending.approval_id, user_id="42",
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()

