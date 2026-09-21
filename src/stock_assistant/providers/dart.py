from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Callable

from ..models import Evidence
from .http import AuthenticationError, ProviderError, RateLimitError, UpstreamSchemaError, get_json


class DartError(ProviderError):
    pass


DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
SEOUL = timezone.utc  # Publication date is date-only; keep a conservative UTC boundary.


def normalize_dart_filings(payload: dict[str, Any], *, observed_at: datetime) -> list[Evidence]:
    status = str(payload.get("status", ""))
    if status == "013":
        return []
    if status in {"010", "011", "012", "901"}:
        raise AuthenticationError(f"DART authentication error: {status}")
    if status == "020":
        raise RateLimitError("DART request limit exceeded")
    if status != "000":
        raise DartError(f"DART returned status {status or 'missing'}")
    rows = payload.get("list")
    if not isinstance(rows, list):
        raise UpstreamSchemaError("DART response requires list array")
    results: list[Evidence] = []
    seen_receipts: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UpstreamSchemaError(f"DART row {index} must be an object")
        required = ("corp_name", "report_nm", "rcept_no", "rcept_dt")
        if any(not str(row.get(field, "")).strip() for field in required):
            raise UpstreamSchemaError(f"DART row {index} is missing required fields")
        receipt = str(row["rcept_no"])
        if receipt in seen_receipts:
            continue
        seen_receipts.add(receipt)
        try:
            published_date = datetime.strptime(str(row["rcept_dt"]), "%Y%m%d").date()
        except ValueError as exc:
            raise UpstreamSchemaError(f"DART row {index} has invalid rcept_dt") from exc
        published_at = datetime.combine(published_date, time.min, timezone.utc)
        if observed_at.astimezone(timezone.utc) < published_at:
            raise UpstreamSchemaError("DART filing observed before its publication date")
        results.append(Evidence(
            source_type="DART",
            title=f"{row['corp_name']} - {row['report_nm']}",
            url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt}",
            published_at=published_at,
            observed_at=observed_at,
            official=True,
            facts=(f"접수번호 {receipt}",),
        ))
    return results


class DartClient:
    def __init__(self, api_key: str, *, fetch_json: Callable[..., Any] = get_json) -> None:
        if not api_key.strip():
            raise AuthenticationError("DART API key is required")
        self.api_key = api_key
        self.fetch_json = fetch_json

    def filings(
        self,
        *,
        observed_at: datetime,
        corp_code: str | None = None,
        begin_date: str | None = None,
        end_date: str | None = None,
        page_no: int = 1,
        page_count: int = 100,
    ) -> list[Evidence]:
        if not (1 <= page_count <= 100):
            raise ValueError("page_count must be between 1 and 100")
        query = {
            "crtfc_key": self.api_key,
            "page_no": str(page_no),
            "page_count": str(page_count),
            "sort": "date",
            "sort_mth": "desc",
        }
        if corp_code:
            query["corp_code"] = corp_code
        if begin_date:
            query["bgn_de"] = begin_date
        if end_date:
            query["end_de"] = end_date
        response = self.fetch_json(DART_LIST_URL, query=query)
        return normalize_dart_filings(response.payload, observed_at=observed_at)

