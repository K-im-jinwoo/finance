from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from stock_assistant.models import Decision, ScreeningResult
from stock_assistant.reports import build_candidate_report, build_journal_draft


UTC = timezone.utc


class ReportTests(unittest.TestCase):
    def test_report_id_is_stable_and_fact_inference_assumption_are_separate(self) -> None:
        result = ScreeningResult(
            "005930", datetime(2026, 9, 21, tzinfo=UTC), Decision.CANDIDATE,
            Decimal("80"), ("OCF_POSITIVE",), (), (), {},
        )
        first = build_candidate_report(
            datetime(2026, 9, 21, tzinfo=UTC), [result],
            facts=("DART 공시",), inferences=("실적 모멘텀",), assumptions=("업황 지속",),
            unavailable=("장중 데이터",),
        )
        second = build_candidate_report(datetime(2026, 9, 21, tzinfo=UTC), [result])
        self.assertNotEqual(first.report_id, second.report_id)
        repeated = build_candidate_report(
            datetime(2026, 9, 21, tzinfo=UTC), [result],
            facts=("DART 공시",), inferences=("실적 모멘텀",), assumptions=("업황 지속",),
            unavailable=("장중 데이터",),
        )
        self.assertEqual(first.report_id, repeated.report_id)
        self.assertEqual(first.fact_summary, ("DART 공시",))
        self.assertEqual(first.unavailable, ("장중 데이터",))
        self.assertEqual(first.ruleset_version, "2026-09-21.1")
        self.assertEqual(first.contract_version, "1.1")

    def test_journal_path_is_server_generated_and_schema_compatible(self) -> None:
        draft = build_journal_draft(
            event_date=date(2026, 9, 21), symbol="007660", company_name="이수페타시스",
            event_slug="position-review", report_id="R-1",
            confirmed_facts=("평단 102000원",), decisions=("추가매수 보류",),
        )
        self.assertEqual(
            draft.relative_path,
            "wiki/20_Areas/Investments/2026-09-21-007660-position-review.md",
        )
        self.assertIn("domains:\n  - finance", draft.markdown)
        self.assertIn("## 확인된 사실", draft.markdown)
        self.assertNotIn("..", draft.relative_path)
        with self.assertRaisesRegex(ValueError, "event_slug"):
            build_journal_draft(
                event_date=date(2026, 9, 21), symbol="007660", company_name="이수",
                event_slug="../../escape", report_id="R-1", confirmed_facts=(), decisions=(),
            )


if __name__ == "__main__":
    unittest.main()
