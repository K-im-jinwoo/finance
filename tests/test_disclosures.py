from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stock_assistant.disclosures import classify_dart_filing_signals
from stock_assistant.models import CatalystStatus, Evidence


UTC = timezone.utc
AS_OF = datetime(2026, 9, 21, tzinfo=UTC)


def filing(title: str, receipt: str, *, official: bool = True) -> Evidence:
    published_at = datetime(2026, 9, 18, 14, 59, 59, tzinfo=UTC)
    return Evidence(
        "DART", title,
        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
        published_at, AS_OF, official,
    )


class DisclosureClassificationTests(unittest.TestCase):
    def test_contract_earnings_and_management_titles_stay_conservative(self) -> None:
        catalysts, risks = classify_dart_filing_signals("005930", [
            filing("회사 - 단일판매ㆍ공급계약체결", "20260918000001"),
            filing("회사 - 영업(잠정)실적(공정공시)", "20260918000002"),
            filing("회사 - 횡령ㆍ배임혐의발생", "20260918000003"),
        ], as_of=AS_OF)
        self.assertEqual([item.status for item in catalysts], [CatalystStatus.PARTIAL, CatalystStatus.PARTIAL])
        self.assertEqual({item.category for item in catalysts}, {"CONTRACT", "EARNINGS"})
        self.assertEqual(len(risks), 1)
        self.assertFalse(risks[0].confirmed)

    def test_contract_termination_is_expired_and_nonofficial_items_are_ignored(self) -> None:
        catalysts, risks = classify_dart_filing_signals("005930", [
            filing("회사 - 단일판매ㆍ공급계약해지", "20260918000004"),
            filing("회사 - 단일판매ㆍ공급계약체결", "20260918000005", official=False),
        ], as_of=AS_OF)
        self.assertEqual(len(catalysts), 1)
        self.assertEqual(catalysts[0].status, CatalystStatus.EXPIRED)
        self.assertEqual(risks, [])


if __name__ == "__main__":
    unittest.main()
