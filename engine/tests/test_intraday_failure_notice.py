from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from stock_assistant.cli import main
from stock_assistant.models import MarketQuote
from stock_assistant.providers.http import AuthenticationError, NetworkError, RateLimitError


NOW = datetime(2026, 10, 6, 3, 30, tzinfo=timezone.utc)


class IntradayFailureNoticeTests(unittest.TestCase):
    def run_failure(self, error, *, alerts_only=True, stage="prices"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("synthetic-client-id", encoding="utf-8")
            client_secret.write_text("synthetic-client-secret", encoding="utf-8")
            args = [
                "refresh-intraday", "--database", str(root / "stock.sqlite3"),
                "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                "--symbols", "005930",
            ]
            if alerts_only:
                args.append("--alerts-only")
            with patch("stock_assistant.cli.datetime") as clock, patch(
                "stock_assistant.cli.TossMarketDataClient"
            ) as client_type, patch("sys.stdout", new_callable=StringIO) as output, patch(
                "sys.stderr", new_callable=StringIO
            ) as errors:
                clock.now.return_value = NOW
                client = client_type.return_value
                if stage == "candles":
                    client.prices.return_value = [MarketQuote(
                        "005930", Decimal("72000"), "KRW", NOW, NOW, "TOSS_SECURITIES_OPEN_API",
                    )]
                getattr(client, stage).side_effect = error
                code = main(args)
            return code, output.getvalue(), errors.getvalue(), client

    def test_rate_limit_explains_failure_in_korean_and_retains_failure_exit(self):
        for stage in ("prices", "candles"):
            with self.subTest(stage=stage):
                code, output, errors, client = self.run_failure(
                    RateLimitError("provider returned HTTP 429"), stage=stage,
                )
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("장중 시세·거래량 조회 실패", errors)
                self.assertIn("2026-10-06T12:30:00+09:00", errors)
                self.assertIn("원인: 토스증권 API 호출 제한 (HTTP 429)", errors)
                self.assertIn("API가 요청 한도 초과로 이번 조회 요청을 거절했습니다.", errors)
                self.assertIn("확인을 완료하지 못했습니다", errors)
                getattr(client, stage).assert_called_once()
                if stage == "prices":
                    client.candles.assert_not_called()

    def test_rate_limit_notice_does_not_disclose_raw_error_or_credentials(self):
        _, output, errors, _ = self.run_failure(RateLimitError(
            "synthetic-client-id synthetic-client-secret Bearer synthetic-access-token",
        ))
        for sensitive in ("synthetic-client-id", "synthetic-client-secret", "synthetic-access-token", "Bearer"):
            self.assertNotIn(sensitive, output + errors)

    def test_full_mode_keeps_json_error_contract(self):
        code, output, errors, client = self.run_failure(
            RateLimitError("provider returned HTTP 429"), alerts_only=False,
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(json.loads(errors), {"error": "provider returned HTTP 429"})
        client.prices.assert_called_once()

    def test_other_failures_are_not_misclassified_as_rate_limits(self):
        for error in (AuthenticationError("provider returned HTTP 401"), NetworkError("provider returned HTTP 503")):
            with self.subTest(error=error):
                code, output, errors, _ = self.run_failure(error)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertEqual(json.loads(errors), {"error": str(error)})
                self.assertNotIn("HTTP 429", errors)

    def test_empty_credentials_fail_without_a_provider_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_id = root / "id"
            client_secret = root / "secret"
            client_id.write_text("", encoding="utf-8")
            client_secret.write_text("", encoding="utf-8")
            with patch("stock_assistant.cli.TossMarketDataClient") as client_type, patch(
                "sys.stdout", new_callable=StringIO
            ) as output, patch("sys.stderr", new_callable=StringIO) as errors:
                code = main([
                    "refresh-intraday", "--database", str(root / "stock.sqlite3"),
                    "--client-id-file", str(client_id), "--client-secret-file", str(client_secret),
                    "--symbols", "005930", "--alerts-only",
                ])
            self.assertEqual(code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(errors.getvalue()), {"error": "Toss credential files cannot be empty"})
            client_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
