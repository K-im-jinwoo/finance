from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..models import AssetType, CompanyKind, EtfSnapshot, Market, OHLCV, Security
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
    etf_snapshots: tuple[EtfSnapshot, ...] = ()


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


def classify_etf_asset_type(name: str) -> AssetType:
    normalized = name.casefold().replace(" ", "")
    if "인버스" in normalized or "곱버스" in normalized:
        return AssetType.INVERSE_ETF
    if "레버리지" in normalized or "2x" in normalized:
        return AssetType.LEVERAGED_ETF
    return AssetType.ETF


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-"}:
        return None
    try:
        parsed = Decimal(str(value).replace(",", "").replace("%", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise UpstreamSchemaError(f"{field} must be numeric when present") from exc
    if not parsed.is_finite():
        raise UpstreamSchemaError(f"{field} must be finite")
    return parsed


def _is_non_trading_row(row: dict[str, Any]) -> bool:
    """Detect KRX's zero-volume sentinel, which is not a valid OHLCV bar."""
    volume = _optional_decimal(row.get("ACC_TRDVOL", row.get("volume")), "ACC_TRDVOL")
    prices = tuple(
        _optional_decimal(row.get(provider_name, row.get(normalized_name)), provider_name)
        for provider_name, normalized_name in (
            ("TDD_OPNPRC", "open"),
            ("TDD_HGPRC", "high"),
            ("TDD_LWPRC", "low"),
        )
    )
    return volume == 0 and all(value == 0 for value in prices)


def _is_blank_market_row(row: dict[str, Any]) -> bool:
    fields = (
        ("ACC_TRDVOL", "volume"),
        ("TDD_OPNPRC", "open"),
        ("TDD_HGPRC", "high"),
        ("TDD_LWPRC", "low"),
        ("TDD_CLSPRC", "close"),
    )
    return all(
        str(row.get(provider_name, row.get(normalized_name)) or "").strip() == ""
        for provider_name, normalized_name in fields
    )


def _is_blank_market_payload(rows: list[Any]) -> bool:
    return bool(rows) and all(
        isinstance(row, dict) and _is_blank_market_row(row)
        for row in rows
    )


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
    if _is_blank_market_payload(rows):
        return []
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise UpstreamSchemaError(f"KRX row {index} must be an object")
        if _is_non_trading_row(row):
            continue
        symbol = row.get("ISU_CD") or row.get("ISU_SRT_CD") or row.get("symbol")
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
    market: str | None = None,
) -> KrxDailySnapshot:
    bars = normalize_krx_daily_payload(payload, observed_at=observed_at, source=source)
    rows = payload["OutBlock_1"]
    if _is_blank_market_payload(rows):
        return KrxDailySnapshot(tuple(bars), {}, ())
    names: dict[str, str] = {}
    etf_snapshots: list[EtfSnapshot] = []
    for index, row in enumerate(rows):
        if _is_non_trading_row(row):
            continue
        symbol = str(row.get("ISU_CD") or row.get("ISU_SRT_CD") or row.get("symbol") or "").strip()
        name = str(row.get("ISU_ABBRV") or row.get("ISU_NM") or row.get("name") or "").strip()
        if name:
            if symbol in names and names[symbol] != name:
                raise UpstreamSchemaError(f"KRX row {index} changes a symbol name inside one response")
            names[symbol] = name
        if market == "ETF":
            raw_date = str(row.get("BAS_DD") or row.get("trade_date") or "").replace("-", "")
            trade_date = _compact_date(raw_date, "BAS_DD")
            nav = _optional_decimal(row.get("NAV"), "NAV")
            net_assets = _optional_decimal(row.get("INVSTASST_NETASST_TOTAMT"), "INVSTASST_NETASST_TOTAMT")
            close = _optional_decimal(row.get("TDD_CLSPRC", row.get("close")), "TDD_CLSPRC")
            premium_discount = None
            if nav is not None and close is not None:
                if nav <= 0:
                    raise UpstreamSchemaError("NAV must be positive when present")
                premium_discount = (close / nav - Decimal("1")) * Decimal("100")
            try:
                etf_snapshots.append(EtfSnapshot(
                    symbol=symbol,
                    trade_date=trade_date,
                    observed_at=observed_at,
                    nav_per_share=nav,
                    net_assets=net_assets,
                    premium_discount_pct=premium_discount,
                    tracking_error_pct=None,
                    total_expense_ratio_pct=None,
                    top10_weight_pct=None,
                    source_url=KRX_DAILY_ENDPOINTS["ETF"],
                ))
            except ValueError as exc:
                raise UpstreamSchemaError(f"invalid KRX ETF row {index}: {exc}") from exc
    return KrxDailySnapshot(tuple(bars), names, tuple(etf_snapshots))


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
        return normalize_krx_daily_snapshot(response.payload, observed_at=observed_at, market=market)

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
