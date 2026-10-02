from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from stock_assistant.client import StockClient, StockClientError, read_role_token


class ClientTests(unittest.TestCase):
    def test_role_token_is_read_from_file_and_short_value_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("x" * 32 + "\n", encoding="utf-8")
            self.assertEqual(read_role_token(path), "x" * 32)
            path.write_text("short", encoding="utf-8")
            with self.assertRaisesRegex(StockClientError, "at least 32"):
                read_role_token(path)

    def test_base_url_rejects_embedded_credentials_and_paths(self) -> None:
        token = Path("token")
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            StockClient("http://user:password@example.com", token)
        with self.assertRaisesRegex(ValueError, "path"):
            StockClient("http://example.com/private", token)

    def test_stored_screen_and_candidate_methods_use_repository_routes(self) -> None:
        client = StockClient("http://127.0.0.1:9120", Path("token"))
        as_of = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
        with patch.object(StockClient, "_request", side_effect=[
            {"report": {"report_id": "R-LATEST"}},
            {"result": {"symbol": "007660"}},
            {"report": {"report_id": "R-1"}, "summary": {"scanned": 1}},
        ]) as request:
            self.assertEqual(client.latest_report()["report_id"], "R-LATEST")
            self.assertEqual(client.screen_stored("007660", as_of=as_of)["symbol"], "007660")
            generated = client.generate_candidates(as_of=as_of, limit=5)
        self.assertEqual(generated["report"]["report_id"], "R-1")
        self.assertEqual(request.call_args_list[0].args[1], "/v1/reports/latest")
        self.assertEqual(request.call_args_list[1].args[1], "/v1/repository/screen")
        self.assertEqual(request.call_args_list[2].args[1], "/v1/repository/candidates")

    def test_stored_methods_reject_naive_time_and_invalid_limit(self) -> None:
        client = StockClient("http://127.0.0.1:9120", Path("token"))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            client.screen_stored("007660", as_of=datetime(2026, 9, 22))
        with self.assertRaisesRegex(ValueError, "between 1 and 20"):
            client.generate_candidates(as_of=datetime.now(timezone.utc), limit=0)
        with self.assertRaisesRegex(ValueError, "future"):
            client.screen_stored(
                "007660", as_of=datetime.now(timezone.utc) + timedelta(days=1),
            )


if __name__ == "__main__":
    unittest.main()
