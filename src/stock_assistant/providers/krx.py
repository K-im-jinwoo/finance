from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from ..models import OHLCV
from ..validation import ContractError, parse_krx_ohlcv_rows
from .http import AuthenticationError, ProviderError, UpstreamSchemaError, get_json


class KrxError(ProviderError):
    pass


KRX_DAILY_ENDPOINTS = {
    "KOSPI": "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd",
    "KOSDAQ": "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd",
    "ETF": "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd",
}


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


class KrxClient:
    def __init__(self, auth_key: str, *, fetch_json: Callable[..., Any] = get_json) -> None:
        if not auth_key.strip():
            raise AuthenticationError("KRX auth key is required")
        self.auth_key = auth_key
        self.fetch_json = fetch_json

    def daily(self, market: str, business_date: date, *, observed_at: datetime) -> list[OHLCV]:
        try:
            endpoint = KRX_DAILY_ENDPOINTS[market]
        except KeyError as exc:
            raise ValueError(f"unsupported KRX market: {market}") from exc
        response = self.fetch_json(
            endpoint,
            query={"basDd": business_date.strftime("%Y%m%d")},
            headers={"AUTH_KEY": self.auth_key},
        )
        return normalize_krx_daily_payload(response.payload, observed_at=observed_at)

