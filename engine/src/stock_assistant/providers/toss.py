from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from ..identifiers import require_korean_security_symbol
from ..models import IntradayCandle, MarketQuote
from .http import JsonResponse, UpstreamSchemaError, get_json, post_form_json


TOSS_BASE_URL = "https://openapi.tossinvest.com"
TOSS_SOURCE = "TOSS_SECURITIES_OPEN_API"

FetchJson = Callable[..., JsonResponse]
PostFormJson = Callable[..., JsonResponse]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise UpstreamSchemaError(f"{field} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UpstreamSchemaError(f"{field} is not valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise UpstreamSchemaError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise UpstreamSchemaError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise UpstreamSchemaError(f"{field} must be finite")
    return parsed


def normalize_toss_prices(payload: dict, *, observed_at: datetime) -> list[MarketQuote]:
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    rows = payload.get("result")
    if not isinstance(rows, list) or not rows:
        raise UpstreamSchemaError("Toss prices result must be a non-empty array")
    quotes: list[MarketQuote] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise UpstreamSchemaError("Toss price row must be an object")
        symbol = str(row.get("symbol", ""))
        require_korean_security_symbol(symbol, field="Toss price symbol")
        if symbol in seen:
            raise UpstreamSchemaError(f"duplicate Toss price symbol: {symbol}")
        seen.add(symbol)
        try:
            quotes.append(MarketQuote(
                symbol=symbol,
                last_price=_decimal(row.get("lastPrice"), field="lastPrice"),
                currency=str(row.get("currency", "")),
                source_timestamp=_timestamp(row.get("timestamp"), field="timestamp"),
                observed_at=observed_at,
                source=TOSS_SOURCE,
            ))
        except ValueError as exc:
            raise UpstreamSchemaError(str(exc)) from exc
    return quotes


def normalize_toss_candles(
    payload: dict,
    *,
    symbol: str,
    observed_at: datetime,
    interval: str = "1m",
) -> list[IntradayCandle]:
    require_korean_security_symbol(symbol, field="Toss candle symbol")
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    result = payload.get("result")
    rows = result.get("candles") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise UpstreamSchemaError("Toss candles result.candles must be an array")
    candles: list[IntradayCandle] = []
    seen: set[datetime] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise UpstreamSchemaError("Toss candle row must be an object")
        timestamp = _timestamp(row.get("timestamp"), field="timestamp")
        # Toss can include the next minute boundary as an in-progress placeholder.
        # It is not point-in-time evidence yet, so drop it instead of persisting it.
        if timestamp > observed_at.astimezone(timezone.utc):
            continue
        if timestamp in seen:
            raise UpstreamSchemaError("duplicate Toss candle timestamp")
        seen.add(timestamp)
        try:
            volume = int(str(row.get("volume")))
            candles.append(IntradayCandle(
                symbol=symbol,
                timestamp=timestamp,
                open=_decimal(row.get("openPrice"), field="openPrice"),
                high=_decimal(row.get("highPrice"), field="highPrice"),
                low=_decimal(row.get("lowPrice"), field="lowPrice"),
                close=_decimal(row.get("closePrice"), field="closePrice"),
                volume=volume,
                currency=str(row.get("currency", "")),
                interval=interval,
                observed_at=observed_at,
                source=TOSS_SOURCE,
            ))
        except (TypeError, ValueError) as exc:
            raise UpstreamSchemaError(str(exc)) from exc
    return sorted(candles, key=lambda item: item.timestamp)


class TossMarketDataClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = TOSS_BASE_URL,
        fetch_json: FetchJson = get_json,
        post_form: PostFormJson = post_form_json,
        clock: Clock = _utc_now,
    ) -> None:
        if not client_id.strip() or not client_secret.strip():
            raise ValueError("Toss client_id and client_secret are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._fetch_json = fetch_json
        self._post_form = post_form
        self._clock = clock
        self._access_token: str | None = None

    def _response_observed_at(self, request_started_at: datetime) -> datetime:
        if request_started_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        completed_at = self._clock()
        if completed_at.tzinfo is None:
            raise ValueError("Toss response clock must be timezone-aware")
        return max(
            request_started_at.astimezone(timezone.utc),
            completed_at.astimezone(timezone.utc),
        )

    def _token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        response = self._post_form(
            f"{self._base_url}/oauth2/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        ).payload
        token = response.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise UpstreamSchemaError("Toss token response is missing access_token")
        self._access_token = token.strip()
        return self._access_token

    def prices(self, symbols: list[str], *, observed_at: datetime) -> list[MarketQuote]:
        if not symbols or len(symbols) > 200:
            raise ValueError("symbols must contain between 1 and 200 items")
        normalized = []
        for symbol in symbols:
            normalized.append(require_korean_security_symbol(symbol, field="Toss price symbol"))
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must be unique")
        response = self._fetch_json(
            f"{self._base_url}/api/v1/prices",
            query={"symbols": ",".join(normalized)},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        quotes = normalize_toss_prices(
            response.payload,
            observed_at=self._response_observed_at(observed_at),
        )
        if {item.symbol for item in quotes} != set(normalized):
            raise UpstreamSchemaError("Toss prices response does not match requested symbols")
        return quotes

    def candles(
        self,
        symbol: str,
        *,
        observed_at: datetime,
        count: int = 30,
    ) -> list[IntradayCandle]:
        require_korean_security_symbol(symbol, field="Toss candle symbol")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 200:
            raise ValueError("count must be an integer between 1 and 200")
        response = self._fetch_json(
            f"{self._base_url}/api/v1/candles",
            query={"symbol": symbol, "interval": "1m", "count": str(count), "adjusted": "true"},
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        return normalize_toss_candles(
            response.payload,
            symbol=symbol,
            observed_at=self._response_observed_at(observed_at),
            interval="1m",
        )
