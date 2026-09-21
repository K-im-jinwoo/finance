from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import Holding, to_json_value
from .contract_io import parse_screen_request
from .repository import StockRepository
from .reports import build_candidate_report, build_journal_draft, report_to_dict
from .screening import screen_security, select_top_candidates


MAX_BODY_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


class StockApi:
    def __init__(self, repository: StockRepository, *, shared_secret: str | None = None) -> None:
        self.repository = repository
        self.shared_secret = shared_secret

    def _authorized(self, headers: dict[str, str]) -> bool:
        if self.shared_secret is None:
            return True
        provided = headers.get("authorization", "")
        expected = f"Bearer {self.shared_secret}"
        return hmac.compare_digest(provided, expected)

    def dispatch(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> ApiResponse:
        headers = {key.casefold(): value for key, value in (headers or {}).items()}
        route = urlparse(path).path
        if route == "/health" and method == "GET":
            return ApiResponse(200, {"status": "ok", "service": "stock-assistant", "orders_enabled": False})
        if not self._authorized(headers):
            return ApiResponse(401, {"error": "unauthorized"})
        if "/order" in route.casefold():
            return ApiResponse(403, {"error": "brokerage orders are outside this service"})

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ApiResponse(400, {"error": "invalid JSON body"})

        try:
            if route == "/v1/holdings" and method == "GET":
                return ApiResponse(200, {"contract_version": "1.0", "holdings": to_json_value(self.repository.list_holdings())})
            if route == "/v1/holdings" and method == "POST":
                holding = _parse_holding(payload)
                self.repository.replace_holding(holding)
                return ApiResponse(200, {"status": "saved", "holding": to_json_value(holding)})
            if route == "/v1/journal/preview" and method == "POST":
                draft = _parse_journal_preview(payload)
                return ApiResponse(200, {"draft": to_json_value(draft), "written": False})
            if route == "/v1/screen" and method == "POST":
                request = parse_screen_request(payload)
                result = _screen(request)
                return ApiResponse(200, {"contract_version": "1.0", "result": to_json_value(result)})
            if route == "/v1/candidates" and method == "POST":
                if not isinstance(payload, dict):
                    raise TypeError("body must be an object")
                raw_items = payload.get("items")
                if not isinstance(raw_items, list) or not raw_items:
                    raise TypeError("items must be a non-empty array")
                limit = int(payload.get("limit", 5))
                requests = [parse_screen_request(item) for item in raw_items]
                as_of_values = {item.as_of for item in requests}
                if len(as_of_values) != 1:
                    raise ValueError("all candidate inputs must share the same as_of")
                selected = select_top_candidates([_screen(item) for item in requests], limit=limit)
                report = build_candidate_report(
                    requests[0].as_of,
                    selected,
                    facts=_string_tuple(payload.get("facts"), "facts"),
                    inferences=_string_tuple(payload.get("inferences"), "inferences"),
                    assumptions=_string_tuple(payload.get("assumptions"), "assumptions"),
                    unavailable=_string_tuple(payload.get("unavailable"), "unavailable"),
                )
                self.repository.save_screening_results(report.report_id, selected)
                return ApiResponse(200, {"report": report_to_dict(report)})
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            return ApiResponse(422, {"error": str(exc)})
        return ApiResponse(404, {"error": "route not found"})


def _screen(request):
    return screen_security(
        request.security,
        request.bars,
        as_of=request.as_of,
        financial=request.financial,
        financing_events=request.financing_events,
        management_risks=request.management_risks,
        catalysts=request.catalysts,
    )


def _parse_holding(payload: dict[str, Any]) -> Holding:
    if not isinstance(payload, dict):
        raise TypeError("body must be an object")
    acquired = payload.get("acquired_on")
    return Holding(
        broker=str(payload["broker"]),
        symbol=str(payload["symbol"]),
        quantity=Decimal(str(payload["quantity"])),
        average_price=Decimal(str(payload["average_price"])),
        acquired_on=date.fromisoformat(str(acquired)) if acquired else None,
    )


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise TypeError(f"{field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _parse_journal_preview(payload: dict[str, Any]):
    if not isinstance(payload, dict):
        raise TypeError("body must be an object")
    return build_journal_draft(
        event_date=date.fromisoformat(str(payload["event_date"])),
        symbol=str(payload["symbol"]),
        company_name=str(payload["company_name"]),
        event_slug=str(payload["event_slug"]),
        report_id=str(payload["report_id"]),
        confirmed_facts=_string_tuple(payload.get("confirmed_facts"), "confirmed_facts"),
        decisions=_string_tuple(payload.get("decisions"), "decisions"),
        private_position_lines=_string_tuple(payload.get("private_position_lines"), "private_position_lines"),
    )


def load_secret(secret_file: Path | None) -> str | None:
    if secret_file is None:
        return None
    secret = secret_file.read_text(encoding="utf-8").strip()
    if not secret:
        raise ValueError("shared-secret file is empty")
    return secret


def serve(*, host: str, port: int, database_path: Path, secret_file: Path | None = None) -> None:
    secret = load_secret(secret_file)
    if host not in {"127.0.0.1", "::1", "localhost"} and secret is None:
        raise ValueError("a shared secret is required for non-loopback binding")
    api = StockApi(StockRepository(database_path), shared_secret=secret)

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError:
                self._send(ApiResponse(400, {"error": "invalid Content-Length"}))
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(ApiResponse(413, {"error": "request body too large"}))
                return
            body = self.rfile.read(length) if length else b""
            response = api.dispatch(
                self.command,
                self.path,
                headers={key: value for key, value in self.headers.items()},
                body=body,
            )
            self._send(response)

        def _send(self, response: ApiResponse) -> None:
            encoded = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            # Never log request bodies, credentials, or holdings.
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()
