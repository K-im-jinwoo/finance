from __future__ import annotations

import unittest

from stock_assistant.presentation import render_candidate_report


class PresentationTests(unittest.TestCase):
    def test_desktop_and_telegram_chunks_keep_report_id_and_evidence_boundaries(self) -> None:
        report_id = "R-20260921T0000Z-ABCDEF12"
        report = {
            "report_id": report_id,
            "as_of": "2026-09-21T00:00:00+00:00",
            "fact_summary": ["DART 공시 확인"],
            "inference_summary": ["실적 모멘텀 가능성"],
            "assumptions": ["업황 지속"],
            "unavailable": ["장중 체결 데이터"],
            "candidates": [
                {
                    "symbol": f"{index:06d}",
                    "name": f"회사-{index}",
                    "decision": "CANDIDATE",
                    "score": "80",
                    "metrics": {"dilution_count_5y": 2, "max_dilution_ratio_pct_5y": "12.5"},
                    "reasons": ["OCF_POSITIVE"],
                    "warnings": ["과열 여부 재확인"],
                    "checks": ["다음 공시 확인"],
                }
                for index in range(1, 6)
            ],
        }
        messages = render_candidate_report(report, max_chars=500)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(message.startswith(f"[{report_id}]") for message in messages))
        self.assertTrue(all(len(message) <= 500 for message in messages))
        combined = "\n".join(messages)
        self.assertIn("확인된 사실\n- DART 공시 확인", combined)
        self.assertIn("추론\n- 실적 모멘텀 가능성", combined)
        self.assertIn("확인 불가\n- 장중 체결 데이터", combined)
        self.assertIn("회사-1 (000001)", combined)
        self.assertIn("5년 최대 잠재 희석률(%): 12.5", combined)

    def test_invalid_or_oversized_report_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "report_id"):
            render_candidate_report({"candidates": []})
        with self.assertRaisesRegex(ValueError, "exceeds"):
            render_candidate_report(
                {"report_id": "R-1", "candidates": [], "fact_summary": ["x" * 600]},
                max_chars=500,
            )
