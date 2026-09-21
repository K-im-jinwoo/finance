from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from stock_assistant.http_api import StockApi
from stock_assistant.repository import StockRepository


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


if __name__ == "__main__":
    unittest.main()

