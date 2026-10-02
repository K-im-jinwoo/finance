from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from stock_assistant.http_api import StockApi, load_role_secrets
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
        annual_operating_income=Decimal("100"),
        ttm_operating_income=Decimal("120"),
        ttm_period_end=date(2026, 6, 30),
        ttm_source_url="https://dart.fss.or.kr/ttm",
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
        report_id = first.body["report"]["report_id"]
        fetched = self.api.dispatch(
            "GET", f"/v1/reports/{report_id}", headers=headers,
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["report"], first.body["report"])

    def test_candidate_endpoint_rejects_empty_items(self) -> None:
        response = self.api.dispatch(
            "POST", "/v1/candidates", headers={"Authorization": "Bearer secret"},
            body=b'{"items":[]}',
        )
        self.assertEqual(response.status, 422)

    def test_repository_routes_screen_one_symbol_and_generate_persisted_report(self) -> None:
        headers = {"Authorization": "Bearer secret"}
        request = screen_payload("007660")
        self.api.repository.save_securities([Security(
            "007660", "이수페타시스", Market.KOSPI,
            AssetType.COMMON, CompanyKind.GENERAL, date(2003, 10, 1),
        )])
        self.api.repository.save_bars(make_bars("007660"))
        financial = request["financial"]
        assert financial is not None
        self.api.repository.save_financial_snapshot(FinancialSnapshot(
            "007660", date.fromisoformat(financial["period_end"]),
            datetime.fromisoformat(financial["published_at"]),
            Decimal(financial["operating_income"]), Decimal(financial["operating_cash_flow"]),
            Decimal(financial["free_cash_flow"]),
            tuple(Decimal(value) for value in financial["receivable_turnover"]),
            tuple(Decimal(value) for value in financial["inventory_turnover"]),
            financial["source_url"],
            annual_operating_income=Decimal(financial["annual_operating_income"]),
            ttm_operating_income=Decimal(financial["ttm_operating_income"]),
            ttm_period_end=date.fromisoformat(financial["ttm_period_end"]),
            ttm_source_url=financial["ttm_source_url"],
        ))
        body = json.dumps({
            "symbol": "007660",
            "as_of": "2026-09-20T12:00:00+00:00",
        }).encode("utf-8")
        screened = self.api.dispatch(
            "POST", "/v1/repository/screen", headers=headers, body=body,
        )
        self.assertEqual(screened.status, 200)
        self.assertEqual(screened.body["result"]["symbol"], "007660")

        generated = self.api.dispatch(
            "POST", "/v1/repository/candidates", headers=headers,
            body=json.dumps({
                "as_of": "2026-09-20T12:00:00+00:00", "limit": 5,
            }).encode("utf-8"),
        )
        self.assertEqual(generated.status, 200)
        self.assertEqual(generated.body["summary"]["scanned"], 1)
        report_id = generated.body["report"]["report_id"]
        self.assertEqual(self.api.repository.get_report(report_id), generated.body["report"])

    def test_repository_routes_validate_symbol_time_limit_and_role(self) -> None:
        headers = {"Authorization": "Bearer secret"}
        missing = self.api.dispatch(
            "POST", "/v1/repository/screen", headers=headers,
            body=b'{"symbol":"999999","as_of":"2026-09-20T12:00:00+00:00"}',
        )
        self.assertEqual(missing.status, 422)
        naive = self.api.dispatch(
            "POST", "/v1/repository/candidates", headers=headers,
            body=b'{"as_of":"2026-09-20T12:00:00","limit":5}',
        )
        self.assertEqual(naive.status, 422)
        invalid_limit = self.api.dispatch(
            "POST", "/v1/repository/candidates", headers=headers,
            body=b'{"as_of":"2026-09-20T12:00:00+00:00","limit":0}',
        )
        self.assertEqual(invalid_limit.status, 422)
        future = self.api.dispatch(
            "POST", "/v1/repository/candidates", headers=headers,
            body=json.dumps({
                "as_of": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "limit": 5,
            }).encode("utf-8"),
        )
        self.assertEqual(future.status, 422)
        self.assertIn("future", future.body["error"])

        scheduler = StockApi(self.api.repository, role_secrets={"scheduler": "s" * 32})
        scheduler_headers = {"Authorization": f"Bearer {'s' * 32}"}
        denied = scheduler.dispatch(
            "POST", "/v1/repository/screen", headers=scheduler_headers, body=b"{}",
        )
        self.assertEqual(denied.status, 403)

    def test_latest_report_is_read_only_and_ignores_future_reports(self) -> None:
        past_id = "R-20260920T1200Z-ABCDEF12"
        future_id = "R-20990101T0000Z-ABCDEF12"
        self.api.repository.save_report(
            past_id, "2026-09-20T12:00:00+00:00",
            {"report_id": past_id, "candidates": []},
        )
        self.api.repository.save_report(
            future_id, "2099-01-01T00:00:00+00:00",
            {"report_id": future_id, "candidates": []},
        )
        response = self.api.dispatch(
            "GET", "/v1/reports/latest",
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["report"]["report_id"], past_id)

        specialist = StockApi(
            self.api.repository,
            role_secrets={"market": "m" * 32},
        )
        specialist_headers = {"Authorization": f"Bearer {'m' * 32}"}
        self.assertEqual(specialist.dispatch(
            "GET", "/v1/reports/latest", headers=specialist_headers,
        ).status, 200)
        self.assertEqual(specialist.dispatch(
            "POST", "/v1/repository/candidates", headers=specialist_headers,
            body=b"{}",
        ).status, 403)

    def test_performance_evaluation_and_read_endpoint_share_persisted_result(self) -> None:
        headers = {"Authorization": "Bearer secret"}
        body = json.dumps({
            "items": [screen_payload()], "limit": 1,
        }).encode("utf-8")
        report_response = self.api.dispatch("POST", "/v1/candidates", headers=headers, body=body)
        report_id = report_response.body["report"]["report_id"]
        self.api.repository.save_bars(make_bars("005930", count=300))
        evaluated = self.api.dispatch(
            "POST", "/v1/performance/evaluate", headers=headers,
            body=json.dumps({
                "report_id": report_id,
                "evaluated_at": "2026-12-31T12:00:00+00:00",
                "horizons": [5],
                "round_trip_cost_bps": "30",
            }).encode("utf-8"),
        )
        self.assertEqual(evaluated.status, 200)
        self.assertEqual(evaluated.body["evaluation"]["records"][0]["status"], "COMPLETE")
        fetched = self.api.dispatch(
            "GET", f"/v1/performance/{report_id}", headers=headers,
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(len(fetched.body["records"]), 1)
        self.assertEqual(fetched.body["summary"][0]["sample_count"], 1)

    def test_performance_evaluation_rejects_naive_timestamp(self) -> None:
        response = self.api.dispatch(
            "POST", "/v1/performance/evaluate",
            headers={"Authorization": "Bearer secret"},
            body=json.dumps({
                "report_id": "R-20260921T0000Z-ABCDEF12",
                "evaluated_at": "2026-09-21T12:00:00",
            }).encode("utf-8"),
        )
        self.assertEqual(response.status, 422)

    def test_specialist_role_can_analyze_but_cannot_read_holdings(self) -> None:
        secret = "m" * 32
        api = StockApi(
            self.api.repository,
            role_secrets={"market": secret, "cio": "c" * 32},
        )
        headers = {"Authorization": f"Bearer {secret}"}
        screened = api.dispatch(
            "POST", "/v1/screen", headers=headers,
            body=json.dumps(screen_payload()).encode("utf-8"),
        )
        self.assertEqual(screened.status, 200)
        denied = api.dispatch("GET", "/v1/holdings", headers=headers)
        self.assertEqual(denied.status, 403)

    def test_role_secret_file_rejects_duplicates_and_short_values(self) -> None:
        path = Path(self.temp.name) / "roles.json"
        path.write_text(json.dumps({"cio": "short"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "at least 32"):
            load_role_secrets(path)
        shared = "x" * 32
        path.write_text(json.dumps({"cio": shared, "market": shared}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unique"):
            load_role_secrets(path)


if __name__ == "__main__":
    unittest.main()
