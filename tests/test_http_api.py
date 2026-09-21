from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.http_api import StockApi
from stock_assistant.models import AssetType, CompanyKind, FinancialSnapshot, Market, Security, to_json_value
from stock_assistant.repository import StockRepository
from tests.helpers import make_bars


UTC = timezone.utc


def screen_payload(symbol: str = "005930") -> dict:
    security = Security(symbol, f"회사-{symbol}", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(2020, 1, 1))
    financial = FinancialSnapshot(
        symbol, date(2026, 6, 30), datetime(2026, 8, 14, tzinfo=UTC),
        Decimal("100"), Decimal("80"), Decimal("60"),
        (Decimal("8"), Decimal("8.1")), (Decimal("6"), Decimal("6.2")),
        "https://dart.fss.or.kr/example",
    )
    return {
        "as_of": "2026-09-20T12:00:00+00:00",
        "security": to_json_value(security),
        "bars": to_json_value(make_bars(symbol)),
        "financial": to_json_value(financial),
        "financing_events": [],
        "management_risks": [],
        "catalysts": [],
    }


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        repository = StockRepository(Path(self.temp.name) / "stock.sqlite3")
        self.api = StockApi(repository, shared_secret="secret")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_health_is_public_and_reports_orders_disabled(self) -> None:
        response = self.api.dispatch("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["orders_enabled"])

    def test_private_routes_require_authentication(self) -> None:
        response = self.api.dispatch("GET", "/v1/holdings")
        self.assertEqual(response.status, 401)

    def test_holding_round_trip_and_invalid_json(self) -> None:
        headers = {"Authorization": "Bearer secret"}
        payload = json.dumps({
            "broker": "토스증권", "symbol": "007660", "quantity": "3",
            "average_price": "102000",
        }, ensure_ascii=False).encode("utf-8")
        saved = self.api.dispatch("POST", "/v1/holdings", headers=headers, body=payload)
        self.assertEqual(saved.status, 200)
        listed = self.api.dispatch("GET", "/v1/holdings", headers=headers)
        self.assertEqual(listed.body["holdings"][0]["broker"], "토스증권")
        invalid = self.api.dispatch("POST", "/v1/holdings", headers=headers, body=b"{")
        self.assertEqual(invalid.status, 400)

    def test_any_order_route_is_forbidden(self) -> None:
        response = self.api.dispatch(
            "POST", "/v1/orders", headers={"Authorization": "Bearer secret"}, body=b"{}",
        )
        self.assertEqual(response.status, 403)

    def test_journal_preview_never_writes(self) -> None:
        payload = json.dumps({
            "event_date": "2026-09-21", "symbol": "007660", "company_name": "이수페타시스",
            "event_slug": "position-review", "report_id": "R-1",
            "confirmed_facts": ["평단 102000원"], "decisions": ["추가매수 보류"],
        }, ensure_ascii=False).encode("utf-8")
        response = self.api.dispatch(
            "POST", "/v1/journal/preview",
            headers={"Authorization": "Bearer secret"}, body=payload,
        )
        self.assertEqual(response.status, 200)
        self.assertFalse(response.body["written"])

    def test_screen_and_candidate_endpoints_share_deterministic_contract(self) -> None:
        headers = {"Authorization": "Bearer secret"}
        screened = self.api.dispatch(
            "POST", "/v1/screen", headers=headers,
            body=json.dumps(screen_payload()).encode("utf-8"),
        )
        self.assertEqual(screened.status, 200)
        self.assertEqual(screened.body["result"]["decision"], "CANDIDATE")

        items = [screen_payload(f"{index:06d}") for index in range(1, 7)]
        body = json.dumps({
            "items": items, "limit": 5,
            "facts": ["정규화된 fixture"],
            "inferences": [], "assumptions": [], "unavailable": ["실시간 데이터"],
        }, ensure_ascii=False).encode("utf-8")
        first = self.api.dispatch("POST", "/v1/candidates", headers=headers, body=body)
        second = self.api.dispatch("POST", "/v1/candidates", headers=headers, body=body)
        self.assertEqual(first.status, 200)
        self.assertEqual(len(first.body["report"]["candidates"]), 5)
        self.assertEqual(first.body["report"]["report_id"], second.body["report"]["report_id"])
        self.assertEqual(first.body["report"]["unavailable"], ["실시간 데이터"])

    def test_candidate_endpoint_rejects_empty_items(self) -> None:
        response = self.api.dispatch(
            "POST", "/v1/candidates", headers={"Authorization": "Bearer secret"},
            body=b'{"items":[]}',
        )
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
