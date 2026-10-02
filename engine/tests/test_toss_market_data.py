from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from io import StringIO
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from stock_assistant.intraday import build_intraday_signal, quote_freshness
from stock_assistant.models import (
    AssetType,
    CompanyKind,
    Holding,
    IntradayCandle,
    Market,
    MarketQuote,
    QuoteFreshness,
    Security,
)
from stock_assistant.providers.http import JsonResponse, UpstreamSchemaError
from stock_assistant.providers.toss import TossMarketDataClient, normalize_toss_candles, normalize_toss_prices
from stock_assistant.repository import StockRepository
from stock_assistant.cli import main


UTC = timezone.utc
NOW = datetime(2026, 9, 22, 6, 30, tzinfo=UTC)


class TossMarketDataTests(unittest.TestCase):
    def test_prices_use_oauth_token_and_exact_requested_symbols(self) -> None:
        calls = []

        def fake_post(url, **kwargs):
            calls.append(("POST", url, kwargs))
            return JsonResponse(200, {"access_token": "short-lived-access-token"})

        def fake_get(url, **kwargs):
            calls.append(("GET", url, kwargs))
            return JsonResponse(200, {"result": [
                {"symbol": "005930", "timestamp": "2026-09-22T15:30:00+09:00", "lastPrice": "72000", "currency": "KRW"},
                {"symbol": "007660", "timestamp": "2026-09-22T15:30:00+09:00", "lastPrice": "110300", "currency": "KRW"},
            ]})

        client = TossMarketDataClient(
            "client-id", "client-secret", fetch_json=fake_get, post_form=fake_post,
            clock=lambda: NOW,
        )
        quotes = client.prices(["005930", "007660"], observed_at=NOW)
        self.assertEqual([item.symbol for item in quotes], ["005930", "007660"])
        self.assertEqual(quotes[1].last_price, Decimal("110300"))
        self.assertEqual(calls[0][2]["form"]["grant_type"], "client_credentials")
        self.assertEqual(calls[1][2]["query"], {"symbols": "005930,007660"})
        self.assertEqual(calls[1][2]["headers"], {"Authorization": "Bearer short-lived-access-token"})

    def test_price_observation_time_is_captured_after_response(self) -> None:
        response_completed_at = NOW + timedelta(seconds=3)

        def fake_post(url, **kwargs):
            return JsonResponse(200, {"access_token": "token"})

        def fake_get(url, **kwargs):
            return JsonResponse(200, {"result": [{
                "symbol": "005930",
                "timestamp": (NOW + timedelta(seconds=2)).isoformat(),
                "lastPrice": "72000",
                "currency": "KRW",
            }]})

        client = TossMarketDataClient(
            "id", "secret", fetch_json=fake_get, post_form=fake_post,
            clock=lambda: response_completed_at,
        )
        quote = client.prices(["005930"], observed_at=NOW)[0]
        self.assertEqual(quote.observed_at, response_completed_at)

    def test_price_after_response_completion_is_still_rejected(self) -> None:
        def fake_post(url, **kwargs):
            return JsonResponse(200, {"access_token": "token"})

        def fake_get(url, **kwargs):
            return JsonResponse(200, {"result": [{
                "symbol": "005930",
                "timestamp": (NOW + timedelta(seconds=4)).isoformat(),
                "lastPrice": "72000",
                "currency": "KRW",
            }]})

        client = TossMarketDataClient(
            "id", "secret", fetch_json=fake_get, post_form=fake_post,
            clock=lambda: NOW + timedelta(seconds=3),
        )
        with self.assertRaisesRegex(UpstreamSchemaError, "after observed_at"):
            client.prices(["005930"], observed_at=NOW)

    def test_prices_fail_closed_on_missing_or_duplicate_symbols(self) -> None:
        payload = {"result": [
            {"symbol": "005930", "timestamp": "2026-09-22T15:30:00+09:00", "lastPrice": "72000", "currency": "KRW"},
            {"symbol": "005930", "timestamp": "2026-09-22T15:30:01+09:00", "lastPrice": "72100", "currency": "KRW"},
        ]}
        with self.assertRaisesRegex(UpstreamSchemaError, "duplicate"):
            normalize_toss_prices(payload, observed_at=NOW)

        def fake_post(url, **kwargs):
            return JsonResponse(200, {"access_token": "token"})

        def fake_get(url, **kwargs):
            return JsonResponse(200, {"result": [payload["result"][0]]})

        client = TossMarketDataClient("id", "secret", fetch_json=fake_get, post_form=fake_post)
        with self.assertRaisesRegex(UpstreamSchemaError, "requested symbols"):
            client.prices(["005930", "007660"], observed_at=NOW)

    def test_candles_are_validated_and_sorted_oldest_first(self) -> None:
        payload = {"result": {"candles": [
            {"timestamp": "2026-09-22T15:29:00+09:00", "openPrice": "100", "highPrice": "105", "lowPrice": "99", "closePrice": "103", "volume": "300", "currency": "KRW"},
            {"timestamp": "2026-09-22T15:28:00+09:00", "openPrice": "99", "highPrice": "101", "lowPrice": "98", "closePrice": "100", "volume": "100", "currency": "KRW"},
        ]}}
        candles = normalize_toss_candles(payload, symbol="005930", observed_at=NOW)
        self.assertLess(candles[0].timestamp, candles[1].timestamp)
        self.assertEqual(candles[1].volume, 300)

    def test_future_provider_timestamp_is_rejected(self) -> None:
        payload = {"result": [{
            "symbol": "005930", "timestamp": "2026-09-22T15:31:00+09:00",
            "lastPrice": "72000", "currency": "KRW",
        }]}
        with self.assertRaisesRegex(UpstreamSchemaError, "after observed_at"):
            normalize_toss_prices(payload, observed_at=NOW)

    def test_future_in_progress_candle_is_dropped(self) -> None:
        payload = {"result": {"candles": [
            {"timestamp": "2026-09-22T15:31:00+09:00", "openPrice": "100", "highPrice": "101", "lowPrice": "99", "closePrice": "100", "volume": "1", "currency": "KRW"},
            {"timestamp": "2026-09-22T15:29:00+09:00", "openPrice": "100", "highPrice": "101", "lowPrice": "99", "closePrice": "100", "volume": "2", "currency": "KRW"},
        ]}}
        candles = normalize_toss_candles(payload, symbol="005930", observed_at=NOW)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].volume, 2)

    def test_freshness_and_closed_candle_volume_spike_are_deterministic(self) -> None:
        quote = MarketQuote(
            "005930", Decimal("72000"), "KRW", NOW - timedelta(seconds=120), NOW,
            "TOSS_SECURITIES_OPEN_API",
        )
        freshness, age = quote_freshness(quote, now=NOW)
        self.assertEqual(freshness, QuoteFreshness.FRESH)
        self.assertEqual(age, 120)

        candles = []
        for index in range(21):
            timestamp = NOW - timedelta(minutes=22 - index)
            volume = 400 if index == 20 else 100
            candles.append(IntradayCandle(
                "005930", timestamp, Decimal("100"), Decimal("101"), Decimal("99"),
                Decimal("100"), volume, "KRW", "1m", NOW, "TOSS_SECURITIES_OPEN_API",
            ))
        signal = build_intraday_signal(quote, candles, now=NOW)
        self.assertEqual(signal.volume_ratio, Decimal("4"))
        self.assertEqual(signal.volume_candle_at, NOW - timedelta(minutes=2))
        self.assertIn("ONE_MINUTE_VOLUME_SPIKE", signal.warnings)

    def test_current_unclosed_candle_is_not_used_for_spike(self) -> None:
        quote = MarketQuote(
            "005930", Decimal("72000"), "KRW", NOW, NOW,
            "TOSS_SECURITIES_OPEN_API",
        )
        candles = [
            IntradayCandle(
                "005930", NOW - timedelta(minutes=21 - index), Decimal("100"), Decimal("101"),
                Decimal("99"), Decimal("100"), 100, "KRW", "1m", NOW,
                "TOSS_SECURITIES_OPEN_API",
            )
            for index in range(21)
        ]
        candles.append(IntradayCandle(
            "005930", NOW, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
            10000, "KRW", "1m", NOW, "TOSS_SECURITIES_OPEN_API",
        ))
        signal = build_intraday_signal(quote, candles, now=NOW)
        self.assertEqual(signal.volume_ratio, Decimal("1"))
        self.assertNotIn("ONE_MINUTE_VOLUME_SPIKE", signal.warnings)

    def test_repository_keeps_quotes_candles_and_watch_scope_point_in_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = StockRepository(Path(directory) / "stock.sqlite3")
            repository.replace_holding(Holding("TOSS", "007660", Decimal("1"), Decimal("102000")))
            report_id = "R-20260922T0600Z-ABCDEF12"
            repository.save_report(report_id, "2026-09-22T06:00:00+00:00", {
                "report_id": report_id,
                "candidates": [{"symbol": "005930"}, {"symbol": "000660"}],
            })
            self.assertEqual(
                repository.watch_symbols(as_of=NOW, candidate_limit=1),
                ["005930", "007660"],
            )

            quote = MarketQuote(
                "007660", Decimal("110300"), "KRW", NOW - timedelta(seconds=10), NOW,
                "TOSS_SECURITIES_OPEN_API",
            )
            candle = IntradayCandle(
                "007660", NOW - timedelta(minutes=2), Decimal("110000"), Decimal("111000"),
                Decimal("109500"), Decimal("110300"), 1000, "KRW", "1m", NOW,
                "TOSS_SECURITIES_OPEN_API",
            )
            repository.save_market_quotes([quote])
            repository.save_intraday_candles([candle])
            self.assertIsNone(repository.latest_market_quote(
                "007660", as_of=NOW - timedelta(minutes=1),
            ))
            self.assertEqual(repository.latest_market_quote("007660", as_of=NOW), quote)
            self.assertEqual(repository.intraday_candles_for("007660", as_of=NOW), [candle])

    def test_full_refresh_includes_stored_security_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stock.sqlite3"
            repository = StockRepository(database)
            repository.save_securities([
                Security("007660", "이수페타시스", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(2003, 10, 7)),
            ])
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("id", encoding="utf-8")
            client_secret.write_text("secret", encoding="utf-8")
            quote = MarketQuote("007660", Decimal("115800"), "KRW", NOW, NOW, "TOSS_SECURITIES_OPEN_API")
            candles = [IntradayCandle(
                "007660", NOW - timedelta(minutes=30 - index), Decimal("100"), Decimal("101"),
                Decimal("99"), Decimal("100"), 100, "KRW", "1m", NOW,
                "TOSS_SECURITIES_OPEN_API",
            ) for index in range(30)]
            with patch("stock_assistant.cli.datetime") as clock, patch(
                "stock_assistant.cli.TossMarketDataClient"
            ) as client_type, patch("sys.stdout", new_callable=StringIO) as output:
                clock.now.return_value = NOW
                client = client_type.return_value
                client.prices.return_value = [quote]
                client.candles.return_value = candles
                code = main([
                    "refresh-intraday", "--database", str(database),
                    "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                    "--symbols", "007660",
                ])
            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["signals"][0]["name"], "이수페타시스")

    def test_alerts_only_is_silent_without_actionable_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stock.sqlite3"
            repository = StockRepository(database)
            report_id = "R-20260922T0600Z-ABCDEF12"
            repository.save_report(report_id, "2026-09-22T06:00:00+00:00", {
                "report_id": report_id, "candidates": [{"symbol": "005930"}],
            })
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("id", encoding="utf-8")
            client_secret.write_text("secret", encoding="utf-8")
            quote = MarketQuote("005930", Decimal("72000"), "KRW", NOW, NOW, "TOSS_SECURITIES_OPEN_API")
            candles = [IntradayCandle(
                "005930", NOW - timedelta(minutes=30 - index), Decimal("100"), Decimal("101"),
                Decimal("99"), Decimal("100"), 100, "KRW", "1m", NOW, "TOSS_SECURITIES_OPEN_API",
            ) for index in range(30)]
            with patch("stock_assistant.cli.datetime") as clock, patch(
                "stock_assistant.cli.TossMarketDataClient"
            ) as client_type, patch("sys.stdout", new_callable=StringIO) as output:
                clock.now.return_value = NOW
                client = client_type.return_value
                client.prices.return_value = [quote]
                client.candles.return_value = candles
                code = main([
                    "refresh-intraday", "--database", str(database),
                    "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                    "--alerts-only",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")

    def test_alerts_only_emits_kst_warning_once_and_suppresses_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "stock.sqlite3"
            repository = StockRepository(database)
            report_id = "R-20260922T0600Z-ABCDEF12"
            repository.save_report(report_id, "2026-09-22T06:00:00+00:00", {
                "report_id": report_id, "candidates": [{"symbol": "005930"}],
            })
            repository.save_securities([
                Security("005930", "삼성전자", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(1975, 6, 11)),
            ])
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("id", encoding="utf-8")
            client_secret.write_text("secret", encoding="utf-8")
            quote = MarketQuote("005930", Decimal("72000"), "KRW", NOW, NOW, "TOSS_SECURITIES_OPEN_API")
            candles = [IntradayCandle(
                "005930", NOW - timedelta(minutes=30 - index), Decimal("100"), Decimal("101"),
                Decimal("99"), Decimal("100"), 400 if index == 29 else 100,
                "KRW", "1m", NOW, "TOSS_SECURITIES_OPEN_API",
            ) for index in range(30)]
            argv = [
                "refresh-intraday", "--database", str(database),
                "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                "--alerts-only",
            ]
            outputs = []
            for run_now in (NOW, NOW + timedelta(minutes=31)):
                with patch("stock_assistant.cli.datetime") as clock, patch(
                    "stock_assistant.cli.TossMarketDataClient"
                ) as client_type, patch("sys.stdout", new_callable=StringIO) as output:
                    clock.now.return_value = run_now
                    client = client_type.return_value
                    client.prices.return_value = [quote]
                    client.candles.return_value = candles
                    code = main(argv)
                self.assertEqual(code, 0)
                outputs.append(output.getvalue())

            self.assertIn("장중 거래량 경고", outputs[0])
            self.assertIn("종목: 삼성전자(005930)", outputs[0])
            self.assertIn("4.00배", outputs[0])
            self.assertIn("2026-09-22T15:30:00+09:00", outputs[0])
            self.assertIn("거래량 기준봉: 2026-09-22T15:29+09:00", outputs[0])
            self.assertEqual(outputs[1], "")

    def test_alerts_only_is_silent_without_watch_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("id", encoding="utf-8")
            client_secret.write_text("secret", encoding="utf-8")
            with patch("stock_assistant.cli.datetime") as clock, patch(
                "sys.stdout", new_callable=StringIO,
            ) as output:
                clock.now.return_value = NOW
                code = main([
                    "refresh-intraday", "--database", str(root / "stock.sqlite3"),
                    "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                    "--alerts-only",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
