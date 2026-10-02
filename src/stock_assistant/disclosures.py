from __future__ import annotations

from datetime import datetime

from .identifiers import require_korean_security_symbol
from .models import Catalyst, CatalystStatus, Evidence, ManagementRisk
from .validation import assert_point_in_time


def classify_dart_filing_signals(
    symbol: str,
    filings: tuple[Evidence, ...] | list[Evidence],
    *,
    as_of: datetime,
) -> tuple[list[Catalyst], list[ManagementRisk]]:
    """Classify only conservative title-level signals; detailed validity stays unresolved."""
    require_korean_security_symbol(symbol)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    catalysts: list[Catalyst] = []
    risks: list[ManagementRisk] = []
    seen_urls: set[str] = set()
    for filing in filings:
        assert_point_in_time(
            as_of,
            observed_at=filing.observed_at,
            published_at=filing.published_at,
            label="DART filing signal",
        )
        if not filing.official or filing.source_type != "DART" or filing.url in seen_urls:
            continue
        seen_urls.add(filing.url)
        title = filing.title.replace(" ", "")
        if "단일판매ㆍ공급계약해지" in title:
            catalysts.append(Catalyst(
                symbol, "CONTRACT", CatalystStatus.EXPIRED,
                filing.published_at, filing.published_at, (filing,),
            ))
        elif "단일판매ㆍ공급계약체결" in title:
            catalysts.append(Catalyst(
                symbol, "CONTRACT", CatalystStatus.PARTIAL,
                filing.published_at, None, (filing,),
            ))
        elif "영업(잠정)실적" in title or "매출액또는손익구조" in title:
            catalysts.append(Catalyst(
                symbol, "EARNINGS", CatalystStatus.PARTIAL,
                filing.published_at, None, (filing,),
            ))
        if "횡령" in title or "배임" in title:
            risks.append(ManagementRisk(
                symbol=symbol,
                confirmed=False,
                category="OFFICIAL_EMBEZZLEMENT_OR_BREACH_OF_TRUST_REVIEW",
                evidence=(filing,),
            ))
    catalysts.sort(key=lambda item: (item.announced_at, item.category))
    risks.sort(key=lambda item: item.evidence[0].published_at)
    return catalysts, risks
