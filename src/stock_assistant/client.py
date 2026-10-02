from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .validation import validate_analysis_time


class StockClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def read_role_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StockClientError("role token file cannot be read") from exc
    if len(token) < 32:
        raise StockClientError("role token must contain at least 32 characters")
    return token


@dataclass(frozen=True, slots=True)
class StockClient:
    base_url: str
    token_file: Path
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an HTTP(S) origin without embedded credentials")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a path, query, or fragment")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        token = read_role_token(self.token_file)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url.rstrip('/')}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "request failed")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = "request failed"
            raise StockClientError(str(detail), status=exc.code) from exc
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StockClientError("stock service is unavailable or returned invalid JSON") from exc

    def get_report(self, report_id: str) -> dict[str, Any]:
        if not report_id.startswith("R-"):
            raise ValueError("report_id is invalid")
        return self._request("GET", f"/v1/reports/{report_id}")["report"]

    def latest_report(self) -> dict[str, Any]:
        return self._request("GET", "/v1/reports/latest")["report"]

    def list_holdings(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/holdings")["holdings"]

    def screen(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/screen", payload)["result"]

    def candidates(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/candidates", payload)["report"]

    def screen_stored(self, symbol: str, *, as_of: datetime) -> dict[str, Any]:
        if not symbol.strip():
            raise ValueError("symbol is required")
        as_of = validate_analysis_time(as_of)
        return self._request("POST", "/v1/repository/screen", {
            "symbol": symbol.strip(),
            "as_of": as_of.isoformat(),
        })["result"]

    def generate_candidates(self, *, as_of: datetime, limit: int = 5) -> dict[str, Any]:
        as_of = validate_analysis_time(as_of)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        return self._request("POST", "/v1/repository/candidates", {
            "as_of": as_of.isoformat(),
            "limit": limit,
        })

    def evaluate_performance(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/performance/evaluate", payload)["evaluation"]

    def get_performance(self, report_id: str) -> dict[str, Any]:
        if not report_id.startswith("R-"):
            raise ValueError("report_id is invalid")
        return self._request("GET", f"/v1/performance/{report_id}")

    def journal_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v1/journal/preview", payload)["draft"]
