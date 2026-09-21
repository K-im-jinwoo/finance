from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Callable
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from ..models import CompanyKind, Evidence, FinancialSnapshot
from .http import AuthenticationError, ProviderError, RateLimitError, UpstreamSchemaError, get_bytes, get_json


class DartError(ProviderError):
    pass


DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_FINANCIAL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_COMPANY_URL = "https://opendart.fss.or.kr/api/company.json"
KST = timezone(timedelta(hours=9))

_OPERATING_INCOME_IDS = {
    "ifrs-full_profitlossfromoperatingactivities",
    "dart_operatingincomeloss",
}
_OPERATING_CASH_FLOW_IDS = {
    "ifrs-full_cashflowsfromusedinoperatingactivities",
    "dart_cashflowsfromusedinoperatingactivities",
}
_CAPEX_ID_FRAGMENTS = (
    "purchaseofpropertyplantandequipment",
    "purchaseofintangibleassets",
    "acquisitionofpropertyplantandequipment",
    "acquisitionofintangibleassets",
)


@dataclass(frozen=True, slots=True)
class DartCompanyProfile:
    kind: CompanyKind
    industry_code: str | None
    fiscal_month: int | None


def _dart_status(payload: dict[str, Any]) -> list[dict[str, Any]]:
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
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UpstreamSchemaError("DART response requires list array of objects")
    return rows


def normalize_dart_corp_codes(payload: bytes) -> dict[str, str]:
    if len(payload) > 50_000_000:
        raise UpstreamSchemaError("DART corporation-code archive is unexpectedly large")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            candidates = [name for name in archive.namelist() if name.casefold().endswith("corpcode.xml")]
            if len(candidates) != 1:
                raise UpstreamSchemaError("DART corporation-code archive must contain one CORPCODE.xml")
            info = archive.getinfo(candidates[0])
            if info.file_size > 100_000_000:
                raise UpstreamSchemaError("DART corporation-code XML is unexpectedly large")
            xml_bytes = archive.read(info)
    except (BadZipFile, KeyError, OSError) as exc:
        raise UpstreamSchemaError("DART corporation-code response is not a valid ZIP archive") from exc
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise UpstreamSchemaError("DART corporation-code XML is invalid") from exc
    mapping: dict[str, str] = {}
    for item in root.findall(".//list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if not stock_code:
            continue
        if not (stock_code.isdigit() and len(stock_code) == 6):
            raise UpstreamSchemaError("DART stock_code must be six digits")
        if not (corp_code.isdigit() and len(corp_code) == 8):
            raise UpstreamSchemaError("DART corp_code must be eight digits")
        if stock_code in mapping and mapping[stock_code] != corp_code:
            raise UpstreamSchemaError(f"duplicate DART stock_code mapping: {stock_code}")
        mapping[stock_code] = corp_code
    if not mapping:
        raise UpstreamSchemaError("DART corporation-code archive contains no listed companies")
    return mapping


def normalize_dart_company_profile(payload: dict[str, Any]) -> DartCompanyProfile:
    status = str(payload.get("status", ""))
    if status in {"010", "011", "012", "901"}:
        raise AuthenticationError(f"DART authentication error: {status}")
    if status == "020":
        raise RateLimitError("DART request limit exceeded")
    if status == "013":
        return DartCompanyProfile(CompanyKind.UNKNOWN, None, None)
    if status != "000":
        raise DartError(f"DART returned status {status or 'missing'}")
    industry_code = str(payload.get("induty_code", "")).strip()
    settlement_month = str(payload.get("acc_mt", "")).strip()
    fiscal_month = None
    if settlement_month:
        if not settlement_month.isdigit() or not 1 <= int(settlement_month) <= 12:
            raise UpstreamSchemaError("DART acc_mt must be a month from 01 to 12")
        fiscal_month = int(settlement_month)
    if not industry_code:
        kind = CompanyKind.UNKNOWN
    else:
        kind = CompanyKind.FINANCIAL if industry_code.startswith(("64", "65", "66")) else CompanyKind.GENERAL
    return DartCompanyProfile(kind, industry_code or None, fiscal_month)


def normalize_dart_company_kind(payload: dict[str, Any]) -> CompanyKind:
    return normalize_dart_company_profile(payload).kind


def _dart_amount(value: Any, field: str) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if text in {"", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise UpstreamSchemaError(f"DART {field} must be numeric") from exc
    if not amount.is_finite():
        raise UpstreamSchemaError(f"DART {field} must be finite")
    return amount


def normalize_dart_financial_statement(
    payload: dict[str, Any],
    *,
    symbol: str,
    period_end: date,
    published_at: datetime,
    source_url: str,
) -> FinancialSnapshot | None:
    rows = _dart_status(payload)
    if not rows:
        return None
    operating_income: Decimal | None = None
    operating_cash_flow: Decimal | None = None
    capex = Decimal("0")
    capex_found = False
    for row in rows:
        account_id = str(row.get("account_id", "")).strip().casefold()
        account_name = str(row.get("account_nm", "")).strip().replace(" ", "")
        amount = _dart_amount(
            row.get("thstrm_add_amount") or row.get("thstrm_amount"),
            "current-period amount",
        )
        if amount is None:
            continue
        if account_id in _OPERATING_INCOME_IDS or account_name in {"영업이익", "영업이익(손실)"}:
            operating_income = amount
        if account_id in _OPERATING_CASH_FLOW_IDS or account_name in {
            "영업활동으로인한현금흐름", "영업활동현금흐름",
        }:
            operating_cash_flow = amount
        if any(fragment in account_id for fragment in _CAPEX_ID_FRAGMENTS) or account_name in {
            "유형자산의취득", "무형자산의취득",
        }:
            capex += abs(amount)
            capex_found = True
    if operating_income is None:
        raise UpstreamSchemaError("DART financial statement has no recognized operating-income account")
    free_cash_flow = None
    if operating_cash_flow is not None and capex_found:
        free_cash_flow = operating_cash_flow - capex
    return FinancialSnapshot(
        symbol=symbol,
        period_end=period_end,
        published_at=published_at,
        operating_income=operating_income,
        operating_cash_flow=operating_cash_flow,
        free_cash_flow=free_cash_flow,
        source_url=source_url,
    )


def normalize_dart_filings(payload: dict[str, Any], *, observed_at: datetime) -> list[Evidence]:
    rows = _dart_status(payload)
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
        published_at = datetime.combine(published_date, time.max, KST).astimezone(timezone.utc)
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
    def __init__(
        self,
        api_key: str,
        *,
        fetch_json: Callable[..., Any] = get_json,
        fetch_bytes: Callable[..., Any] = get_bytes,
    ) -> None:
        if not api_key.strip():
            raise AuthenticationError("DART API key is required")
        self.api_key = api_key
        self.fetch_json = fetch_json
        self.fetch_bytes = fetch_bytes

    def corp_codes(self) -> dict[str, str]:
        response = self.fetch_bytes(DART_CORP_CODE_URL, query={"crtfc_key": self.api_key})
        return normalize_dart_corp_codes(response.payload)

    def company_kind(self, corp_code: str) -> CompanyKind:
        return self.company_profile(corp_code).kind

    def company_profile(self, corp_code: str) -> DartCompanyProfile:
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise ValueError("corp_code must be eight digits")
        response = self.fetch_json(
            DART_COMPANY_URL,
            query={"crtfc_key": self.api_key, "corp_code": corp_code},
        )
        return normalize_dart_company_profile(response.payload)

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

    def financial_statement(
        self,
        *,
        corp_code: str,
        symbol: str,
        business_year: int,
        report_code: str,
        financial_statement_division: str,
        period_end: date,
        published_at: datetime,
    ) -> FinancialSnapshot | None:
        if len(corp_code) != 8 or not corp_code.isdigit():
            raise ValueError("corp_code must be eight digits")
        if report_code not in {"11011", "11012", "11013", "11014"}:
            raise ValueError("unsupported DART report_code")
        if financial_statement_division not in {"CFS", "OFS"}:
            raise ValueError("financial_statement_division must be CFS or OFS")
        response = self.fetch_json(DART_FINANCIAL_URL, query={
            "crtfc_key": self.api_key,
            "corp_code": corp_code,
            "bsns_year": str(business_year),
            "reprt_code": report_code,
            "fs_div": financial_statement_division,
        })
        source_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={_receipt_number(response.payload)}"
        return normalize_dart_financial_statement(
            response.payload,
            symbol=symbol,
            period_end=period_end,
            published_at=published_at,
            source_url=source_url,
        )


def _receipt_number(payload: dict[str, Any]) -> str:
    rows = payload.get("list")
    if not isinstance(rows, list):
        return "unknown"
    for row in rows:
        if isinstance(row, dict) and str(row.get("rcept_no", "")).isdigit():
            return str(row["rcept_no"])
    return "unknown"
