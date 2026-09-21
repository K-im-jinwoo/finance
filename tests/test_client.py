from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
