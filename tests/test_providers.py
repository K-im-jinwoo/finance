from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from stock_assistant.providers.dart import normalize_dart_filings
from stock_assistant.providers.http import AuthenticationError, JsonResponse, RateLimitError, UpstreamSchemaError
from stock_assistant.providers.krx import KrxClient, normalize_krx_daily_payload
from stock_assistant.providers.news import normalize_news_items


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED = datetime(2026, 9, 18, 12, tzinfo=UTC)


class ProviderTests(unittest.TestCase):
    def test_raw_krx_payload_normalizes_prices_and_volume(self) -> None:
        payload = json.loads((FIXTURES / "krx_daily_raw.json").read_text(encoding="utf-8"))
        rows = normalize_krx_daily_payload(payload, observed_at=OBSERVED)
        self.assertEqual(rows[0].symbol, "005930")
        self.assertEqual(str(rows[0].close), "82000")
        self.assertEqual(rows[0].volume, 12_345_678)

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

