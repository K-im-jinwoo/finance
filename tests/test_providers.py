from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from stock_assistant.models import AssetType, CompanyKind
from stock_assistant.providers.dart import (
    DartClient,
    normalize_dart_company_kind,
    normalize_dart_company_profile,
    normalize_dart_corp_codes,
    normalize_dart_filings,
    normalize_dart_financial_statement,
)
from stock_assistant.providers.http import AuthenticationError, BinaryResponse, JsonResponse, RateLimitError, UpstreamSchemaError
from stock_assistant.providers.krx import (
    KrxClient,
    normalize_krx_daily_payload,
    normalize_krx_daily_snapshot,
    normalize_krx_security_payload,
)
from stock_assistant.providers.news import normalize_news_items


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED = datetime(2026, 9, 18, 15, tzinfo=UTC)


class ProviderTests(unittest.TestCase):
    def test_raw_krx_payload_normalizes_prices_and_volume(self) -> None:
        payload = json.loads((FIXTURES / "krx_daily_raw.json").read_text(encoding="utf-8"))
        rows = normalize_krx_daily_payload(payload, observed_at=OBSERVED)
        self.assertEqual(rows[0].symbol, "005930")
        self.assertEqual(str(rows[0].close), "82000")
        self.assertEqual(rows[0].volume, 12_345_678)
        snapshot = normalize_krx_daily_snapshot(payload, observed_at=OBSERVED)
        self.assertEqual(snapshot.names, {"005930": "삼성전자"})

    def test_krx_client_sends_auth_header_and_business_date(self) -> None:
        calls = []
        payload = json.loads((FIXTURES / "krx_daily_raw.json").read_text(encoding="utf-8"))

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return JsonResponse(200, payload)

        client = KrxClient("not-a-real-key", fetch_json=fake_fetch)
        rows = client.daily("KOSPI", OBSERVED.date(), observed_at=OBSERVED)
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls[0][1]["headers"], {"AUTH_KEY": "not-a-real-key"})
        self.assertEqual(calls[0][1]["query"], {"basDd": "20260918"})

    def test_krx_schema_change_fails_closed(self) -> None:
        with self.assertRaises(UpstreamSchemaError):
            normalize_krx_daily_payload({"changed": []}, observed_at=OBSERVED)

    def test_krx_security_universe_distinguishes_common_preferred_and_spac(self) -> None:
        payload = json.loads((FIXTURES / "krx_security_raw.json").read_text(encoding="utf-8"))
        securities = normalize_krx_security_payload(payload, market="KOSPI")
        self.assertEqual(
            [item.asset_type for item in securities],
            [AssetType.COMMON, AssetType.PREFERRED, AssetType.SPAC],
        )
        self.assertTrue(all(item.company_kind is CompanyKind.UNKNOWN for item in securities))
        self.assertEqual(securities[0].listed_on, date(1975, 6, 11))

    def test_krx_security_client_uses_official_basic_info_contract(self) -> None:
        calls = []
        payload = json.loads((FIXTURES / "krx_security_raw.json").read_text(encoding="utf-8"))

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return JsonResponse(200, payload)

        securities = KrxClient("not-a-real-key", fetch_json=fake_fetch).securities("KOSPI", OBSERVED.date())
        self.assertEqual(len(securities), 3)
        self.assertTrue(calls[0][0].endswith("/sto/stk_isu_base_info"))
        self.assertEqual(calls[0][1]["query"], {"basDd": "20260918"})

    def test_dart_normalization_and_error_codes(self) -> None:
        payload = json.loads((FIXTURES / "dart_filings_raw.json").read_text(encoding="utf-8"))
        filings = normalize_dart_filings(payload, observed_at=OBSERVED)
        self.assertTrue(filings[0].official)
        self.assertIn("20260918000001", filings[0].url)
        self.assertEqual(normalize_dart_filings({"status": "013"}, observed_at=OBSERVED), [])
        with self.assertRaises(AuthenticationError):
            normalize_dart_filings({"status": "010"}, observed_at=OBSERVED)
        with self.assertRaises(RateLimitError):
            normalize_dart_filings({"status": "020"}, observed_at=OBSERVED)

    def test_dart_financial_statement_extracts_operating_cash_flow_and_fcf(self) -> None:
        payload = json.loads((FIXTURES / "dart_financial_raw.json").read_text(encoding="utf-8"))
        snapshot = normalize_dart_financial_statement(
            payload,
            symbol="005930",
            period_end=date(2025, 12, 31),
            published_at=OBSERVED,
            source_url="https://dart.fss.or.kr/example",
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.operating_income, Decimal("1000"))
        self.assertEqual(snapshot.operating_cash_flow, Decimal("700"))
        self.assertEqual(snapshot.free_cash_flow, Decimal("450"))

    def test_dart_financial_client_sends_required_point_in_time_query(self) -> None:
        calls = []
        payload = json.loads((FIXTURES / "dart_financial_raw.json").read_text(encoding="utf-8"))

        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return JsonResponse(200, payload)

        snapshot = DartClient("not-a-real-key", fetch_json=fake_fetch).financial_statement(
            corp_code="00126380",
            symbol="005930",
            business_year=2025,
            report_code="11011",
            financial_statement_division="CFS",
            period_end=date(2025, 12, 31),
            published_at=OBSERVED,
        )
        self.assertIsNotNone(snapshot)
        self.assertTrue(calls[0][0].endswith("/fnlttSinglAcntAll.json"))
        self.assertEqual(calls[0][1]["query"]["fs_div"], "CFS")
        self.assertEqual(calls[0][1]["query"]["reprt_code"], "11011")

    def test_dart_corp_code_archive_and_company_kind_are_normalized(self) -> None:
        xml = (
            "<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
            "<stock_code>005930</stock_code><modify_date>20260918</modify_date></list></result>"
        ).encode("utf-8")
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("CORPCODE.xml", xml)
        self.assertEqual(normalize_dart_corp_codes(buffer.getvalue()), {"005930": "00126380"})
        self.assertEqual(
            normalize_dart_company_kind({"status": "000", "induty_code": "64992", "acc_mt": "12"}),
            CompanyKind.FINANCIAL,
        )
        self.assertEqual(
            normalize_dart_company_kind({"status": "000", "induty_code": "26110"}),
            CompanyKind.GENERAL,
        )
        profile = normalize_dart_company_profile({"status": "000", "induty_code": "26110", "acc_mt": "03"})
        self.assertEqual(profile.fiscal_month, 3)

    def test_dart_client_keeps_binary_and_json_credentials_inside_provider_boundary(self) -> None:
        xml = (
            "<result><list><corp_code>00126380</corp_code><stock_code>005930</stock_code>"
            "<modify_date>20260918</modify_date></list></result>"
        ).encode("utf-8")
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            archive.writestr("CORPCODE.xml", xml)
        calls = []

        def fake_bytes(url, **kwargs):
            calls.append(("bytes", url, kwargs))
            return BinaryResponse(200, buffer.getvalue())

        def fake_json(url, **kwargs):
            calls.append(("json", url, kwargs))
            return JsonResponse(200, {"status": "000", "induty_code": "26110"})

        client = DartClient("not-a-real-key", fetch_json=fake_json, fetch_bytes=fake_bytes)
        self.assertEqual(client.corp_codes()["005930"], "00126380")
        self.assertEqual(client.company_kind("00126380"), CompanyKind.GENERAL)
        self.assertEqual(calls[0][2]["query"], {"crtfc_key": "not-a-real-key"})
        self.assertEqual(calls[1][2]["query"]["corp_code"], "00126380")

    def test_news_is_cleaned_and_deduplicated_by_canonical_url_and_title(self) -> None:
        items = [
            {
                "title": "<b>계약 체결</b>",
                "originallink": "https://news.example/item?utm_source=x&id=1",
                "published_at": "2026-09-18T01:00:00+00:00",
            },
            {
                "title": "계약 체결",
                "originallink": "https://news.example/item?id=1&utm_medium=y",
                "published_at": "2026-09-18T01:00:00+00:00",
            },
        ]
        results = normalize_news_items(items, observed_at=OBSERVED)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "계약 체결")
        self.assertEqual(results[0].url, "https://news.example/item?id=1")

    def test_news_requires_timezone_aware_publication(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            normalize_news_items([
                {"title": "title", "url": "https://example.test", "published_at": "2026-09-18T01:00:00"}
            ], observed_at=OBSERVED)


if __name__ == "__main__":
    unittest.main()
