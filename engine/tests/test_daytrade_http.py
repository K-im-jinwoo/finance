import random
import unittest
from stock_assistant.daytrade.http import HttpResult, RateGroup, RestAdapter, safe_error


class HttpTests(unittest.TestCase):
    def test_safe_diagnostics_ignore_messages_data_and_body_request_id(self):
        p = {"error": {"code": "rate-limit-exceeded", "message": "PRIVATE", "data": {"token": "PRIVATE"}, "requestId": "BODY-ID"}}
        self.assertEqual(safe_error(p, {"X-Request-Id": "01HXYZABCDEFG123456789"}),
                         {"api_error_code": "rate-limit-exceeded", "request_id": "01HXYZABCDEFG123456789"})
        self.assertEqual(safe_error(b"not-json", {"X-Request-Id": "Bearer PRIVATE"}), {})
        self.assertEqual(safe_error(p, {"X-Request-Id": "SYNTHETIC-TOKEN"}, secrets=("SYNTHETIC-TOKEN",))["api_error_code"], "rate-limit-exceeded")
        self.assertNotIn("request_id", safe_error(p, {"X-Request-Id": "SYNTHETIC-TOKEN"}, secrets=("SYNTHETIC-TOKEN",)))
        self.assertEqual(safe_error(b"x" * 65537, {}), {})

    def test_positive_remaining_does_not_override_429_and_groups_are_independent(self):
        calls = []
        def transport(path, query):
            calls.append(path)
            return HttpResult(429 if "candles" in path else 200, {}, {"X-RateLimit-Remaining": "19"})
        a = RestAdapter(transport, rng=random.Random(7))
        first = a.fetch("/api/v1/candles", {}, now=0)
        self.assertGreater(first["retry_at"], 1)
        self.assertEqual(a.fetch("/api/v1/candles", {}, now=.5)["status"], "BACKOFF")
        self.assertEqual(a.fetch("/api/v1/rankings", {}, now=.5)["status"], "OK")
        self.assertEqual(len(calls), 2)
        now = first["retry_at"]
        second = a.fetch("/api/v1/candles", {}, now=now)
        self.assertGreater(second["retry_at"] - now, 2)
        third = a.fetch("/api/v1/candles", {}, now=second["retry_at"])
        self.assertEqual(third["status"], "RATE_LIMIT_DEGRADED")
        before = len(calls)
        a.fetch("/api/v1/candles", {}, now=999)
        self.assertEqual(len(calls), before)
        self.assertTrue(a.entry_blocked)
        a.fetch("/api/v1/prices", {}, now=999, purpose="EXIT_MONITOR")
        self.assertTrue(a.entry_blocked)

    def test_recovery_is_explicit_and_requires_success_for_that_group(self):
        response = [HttpResult(429, {}, {"Retry-After": "12"})]
        a = RestAdapter(lambda *args: response[0], rng=random.Random(1), degrade_after=1)
        r = a.fetch("/api/v1/candles", {}, now=0)
        self.assertEqual(r["retry_at"], 12)
        with self.assertRaises(ValueError):
            a.recover(RateGroup.MARKET_DATA_CHART, verified=True)
        response[0] = HttpResult(200, {}, {})
        a.fetch("/api/v1/candles", {}, now=12, purpose="EXIT_MONITOR")
        self.assertTrue(a.entry_blocked)
        a.recover(RateGroup.MARKET_DATA_CHART, verified=True)
        self.assertFalse(a.entry_blocked)

    def test_seeded_backoff_is_reproducible_and_private_paths_are_rejected(self):
        a = [RestAdapter(lambda *args: HttpResult(429, {}, {}), rng=random.Random(9)) for _ in range(2)]
        self.assertEqual(a[0].fetch("/api/v1/prices", {}, now=10), a[1].fetch("/api/v1/prices", {}, now=10))
        with self.assertRaises(ValueError):
            a[0].fetch("/api/v1/orders", {}, now=0)

    def test_transport_errors_do_not_expose_exception_text_and_first_429_blocks_entry(self):
        def broken(*args): raise RuntimeError("SYNTHETIC-SECRET")
        a = RestAdapter(broken, rng=random.Random(1))
        r = a.fetch("/api/v1/prices", {}, now=0)
        self.assertEqual(r["status"], "TRANSPORT_ERROR")
        self.assertNotIn("SECRET", str(r))
        a.transport = lambda *args: HttpResult(429, {}, {})
        self.assertTrue(a.fetch("/api/v1/prices", {}, now=0)["entry_blocked"])
