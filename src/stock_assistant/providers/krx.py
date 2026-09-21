from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from ..models import AssetType, CompanyKind, Market, OHLCV, Security
from ..validation import ContractError, parse_krx_ohlcv_rows
from .http import AuthenticationError, ProviderError, UpstreamSchemaError, get_json


class KrxError(ProviderError):
    pass


KRX_DAILY_ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
    "ETF": "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd",
}
KRX_SECURITY_ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_isu_base_info",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_isu_base_info",
}


@dataclass(frozen=True, slots=True)
class KrxDailySnapshot:
    bars: tuple[OHLCV, ...]
    names: dict[str, str]


def _compact_date(value: Any, field: str) -> date:
    text = str(value or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise UpstreamSchemaError(f"{field} must be YYYYMMDD")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError as exc:
        raise UpstreamSchemaError(f"{field} is not a valid date") from exc


def _asset_type(name: str, certificate_type: str) -> AssetType:
    combined = f"{name} {certificate_type}".casefold()
    if "스팩" in combined or "기업인수목적" in combined:
        return AssetType.SPAC
    if "우선" in certificate_type or certificate_type.strip().startswith("우"):
        return AssetType.PREFERRED
    if "보통" in certificate_type:
        return AssetType.COMMON
    return AssetType.OTHER


def normalize_krx_security_payload(payload: dict[str, Any], *, market: str) -> list[Security]:
    try:
        market_enum = Market(market)
    except ValueError as exc:
        raise ValueError(f"unsupported KRX security market: {market}") from exc
    rows = payload.get("OutBlock_1")
    if not isinstance(rows, list):
        raise UpstreamSchemaError("KRX security response requires OutBlock_1 array")
    results: list[Security] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UpstreamSchemaError(f"KRX security row {index} must be an object")
        symbol = str(row.get("ISU_SRT_CD", "")).strip()
        name = str(row.get("ISU_ABBRV") or row.get("ISU_NM") or "").strip()
        certificate_type = str(row.get("KIND_STKCERT_TP_NM", "")).strip()
        if not symbol or not name or not certificate_type:
            raise UpstreamSchemaError(f"KRX security row {index} is missing identity fields")
        if symbol in seen:
            raise UpstreamSchemaError(f"duplicate KRX security symbol: {symbol}")
        seen.add(symbol)
        try:
            results.append(Security(
                symbol=symbol,
                name=name,
                market=market_enum,
                asset_type=_asset_type(name, certificate_type),
                company_kind=CompanyKind.UNKNOWN,
                listed_on=_compact_date(row.get("LIST_DD"), "LIST_DD"),
            ))
        except ValueError as exc:
            raise UpstreamSchemaError(f"invalid KRX security row {index}: {exc}") from exc
    return sorted(results, key=lambda item: item.symbol)


def normalize_krx_daily_payload(
    payload: dict[str, Any],
    *,
    observed_at: datetime,
    source: str = "KRX_OPEN_API",
) -> list[OHLCV]:
    rows = payload.get("OutBlock_1")
    if not isinstance(rows, list):
        raise UpstreamSchemaError("KRX response requires OutBlock_1 array")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UpstreamSchemaError(f"KRX row {index} must be an object")
        symbol = row.get("ISU_SRT_CD") or row.get("symbol")
        base_date = row.get("BAS_DD") or row.get("trade_date")
        if isinstance(base_date, str) and len(base_date) == 8 and base_date.isdigit():
            base_date = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:]}"
        normalized.append({
            "symbol": symbol,
            "trade_date": base_date,
            "open": row.get("TDD_OPNPRC", row.get("open")),
            "high": row.get("TDD_HGPRC", row.get("high")),
            "low": row.get("TDD_LWPRC", row.get("low")),
            "close": row.get("TDD_CLSPRC", row.get("close")),
            "volume": row.get("ACC_TRDVOL", row.get("volume")),
        })
    try:
        return parse_krx_ohlcv_rows(normalized, observed_at=observed_at, source=source)
    except ContractError as exc:
        raise UpstreamSchemaError(f"invalid KRX daily payload: {exc}") from exc


def normalize_krx_daily_snapshot(
    payload: dict[str, Any],
    *,
    observed_at: datetime,
    source: str = "KRX_OPEN_API",
) -> KrxDailySnapshot:
    bars = normalize_krx_daily_payload(payload, observed_at=observed_at, source=source)
    rows = payload["OutBlock_1"]
    names: dict[str, str] = {}
    for index, row in enumerate(rows):
        symbol = str(row.get("ISU_SRT_CD") or row.get("symbol") or "").strip()
        name = str(row.get("ISU_ABBRV") or row.get("ISU_NM") or row.get("name") or "").strip()
        if name:
            if symbol in names and names[symbol] != name:
                raise UpstreamSchemaError(f"KRX row {index} changes a symbol name inside one response")
            names[symbol] = name
    return KrxDailySnapshot(tuple(bars), names)


class KrxClient:
    def __init__(self, auth_key: str, *, fetch_json: Callable[..., Any] = get_json) -> None:
        if not auth_key.strip():
            raise AuthenticationError("KRX auth key is required")
        self.auth_key = auth_key
        self.fetch_json = fetch_json

    def daily(self, market: str, business_date: date, *, observed_at: datetime) -> list[OHLCV]:
        return list(self.daily_snapshot(market, business_date, observed_at=observed_at).bars)

    def daily_snapshot(
        self,
        market: str,
        business_date: date,
        *,
        observed_at: datetime,
    ) -> KrxDailySnapshot:
        try:
            endpoint = KRX_DAILY_ENDPOINTS[market]
        except KeyError as exc:
            raise ValueError(f"unsupported KRX market: {market}") from exc
        response = self.fetch_json(
            endpoint,
            query={"basDd": business_date.strftime("%Y%m%d")},
            headers={"AUTH_KEY": self.auth_key},
        )
        return normalize_krx_daily_snapshot(response.payload, observed_at=observed_at)

    def securities(self, market: str, business_date: date) -> list[Security]:
        try:
            endpoint = KRX_SECURITY_ENDPOINTS[market]
        except KeyError as exc:
            raise ValueError(f"unsupported KRX security market: {market}") from exc
        response = self.fetch_json(
            endpoint,
            query={"basDd": business_date.strftime("%Y%m%d")},
            headers={"AUTH_KEY": self.auth_key},
        )
        return normalize_krx_security_payload(response.payload, market=market)
