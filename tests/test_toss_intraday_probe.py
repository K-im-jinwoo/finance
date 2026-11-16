import contextlib
from collections import deque
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "engine/src")]
from shared_toss_auth import SharedTokenCache
from toss_intraday_probe import (NoRedirect, ProbeStopped, PublicRestProbe, main,
                                 read_existing_token, sample_rest, sample_websocket, symbols_checked)


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status, self.headers = status, headers or {}
        self.body = json.dumps(payload if payload is not None else {"result": []}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        return self.body[:size]


class ProbeSocketServer:
    """Stdlib socket/clock harness: opening a second live socket is an error."""
    def __init__(self, *, frames=(), drop_pong=False, drop_ack=(), reject_ack=(), close_failure=False, unsubscribe_failure=False):
        self.now = 0
        self.frames, self.drop_pong = frames, drop_pong
        self.drop_ack, self.reject_ack = set(drop_ack), set(reject_ack)
        self.close_failure, self.unsubscribe_failure = close_failure, unsubscribe_failure
        self.sockets, self.trace = [], []

    def clock(self):
        return self.now

    def connect(self, *args, **kwargs):
        if any(not s.closed for s in self.sockets):
            raise AssertionError("overlapping websocket connections")
        sock = ProbeSocket(self, len(self.sockets) + 1)
        self.sockets.append(sock)
        self.trace.append(("connect", sock.index))
        self.now += .01
        return sock


class ProbeSocket:
    def __init__(self, server, index):
        self.server, self.index, self.closed = server, index, False
        self.frames, self.sent = deque(), []

    def send(self, raw):
        self.sent.append(raw)
        if raw == "PING":
            if not self.server.drop_pong:
                self.frames.append(json.dumps({"type": "pong"}))
            return
        declaration = json.loads(raw)
        if not declaration:
            self.server.trace.append(("unsubscribe", self.index))
            if self.server.unsubscribe_failure:
                raise OSError("synthetic unsubscribe failure")
            return
        if self.index not in self.server.drop_ack:
            topics = [row["type"] + ":" + code for row in declaration if "type" in row for code in row["codes"]]
            ack = {"type": "subscriptions", "id": declaration[0]["id"], "subscribed": topics, "rejected": []}
            if self.index in self.server.reject_ack:
                ack["rejected"] = [{"target": topics.pop(), "code": "stock-not-found"}]
            self.frames.append(json.dumps(ack))
        if self.index == 1:
            self.frames.extend(json.dumps(f) for f in self.server.frames)

    def recv(self, *, timeout):
        if self.frames:
            self.server.now += .01
            return self.frames.popleft()
        self.server.now += timeout
        raise TimeoutError()

    def close(self):
        self.server.trace.append(("close", self.index))
        if self.server.close_failure:
            raise OSError("synthetic close failure")
        self.closed = True


class IntradayProbeTests(unittest.TestCase):
    def test_existing_cache_token_reuse_has_no_renewal_or_byte_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = SharedTokenCache(Path(folder).resolve(), "SYNTHETIC-CLIENT", clock=lambda: 1000)
            cache.get(lambda: {"access_token": "SYNTHETIC-TOKEN", "expires_in": 600})
            before = {p.name: p.read_bytes() for p in cache.directory.iterdir()}
            self.assertEqual(read_existing_token(cache, minimum_ttl=100, now=lambda: 1000), "SYNTHETIC-TOKEN")
            self.assertEqual(before, {p.name: p.read_bytes() for p in cache.directory.iterdir()})
            with self.assertRaisesRegex(ProbeStopped, "TTL_INSUFFICIENT"):
                read_existing_token(cache, minimum_ttl=100, now=lambda: 1500)
            self.assertEqual(before, {p.name: p.read_bytes() for p in cache.directory.iterdir()})

    def test_missing_cache_is_not_initialized(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = SharedTokenCache(Path(folder).resolve() / "absent", "SYNTHETIC-CLIENT")
            with self.assertRaisesRegex(ProbeStopped, "EXISTING_SHARED_CACHE_REQUIRED"):
                read_existing_token(cache, minimum_ttl=100)
            self.assertFalse(cache.directory.exists())

    def test_only_public_get_paths_and_bounded_request_count(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: Response()
        probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
        for path in ("/oauth2/token", "/api/v1/orders", "/api/v1/accounts", "https://elsewhere.invalid"):
            with self.assertRaises(ProbeStopped):
                probe.get(path, {})
        opener.open.assert_not_called()
        for _ in range(10):
            probe.get("/api/v1/prices", {"symbols": "005930"})
        with self.assertRaisesRegex(ProbeStopped, "BUDGET_EXCEEDED"):
            probe.get("/api/v1/prices", {"symbols": "005930"})
        self.assertEqual(opener.open.call_count, 10)
        self.assertTrue(all(c.args[0].method == "GET" for c in opener.open.call_args_list))

    def test_429_stops_without_retries_or_token_invalidation(self):
        opener = Mock()
        opener.open.return_value = Response(status=429, headers={"Retry-After": "1"})
        probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
        with self.assertRaisesRegex(ProbeStopped, "PUBLIC_HTTP_429"):
            probe.get("/api/v1/prices", {})
        with self.assertRaisesRegex(ProbeStopped, "ALREADY_STOPPED"):
            probe.get("/api/v1/prices", {})
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(probe.observations[0]["rate_headers"], {"Retry-After": "1"})

    def test_redirect_is_refused_before_authorization_can_be_forwarded(self):
        with self.assertRaisesRegex(ProbeStopped, "REDIRECT_REFUSED"):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")

    def test_non_200_extracts_only_safe_code_and_header_id_without_retry(self):
        opener = Mock()
        opener.open.return_value = Response({"error": {"code": "rate-limit-exceeded", "message": "PRIVATE",
            "data": {"token": "SYNTHETIC-TOKEN"}, "requestId": "BODY-PRIVATE"}}, status=429,
            headers={"X-Request-Id": "01HXYZABCDEFG123456789"})
        probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
        with self.assertRaises(ProbeStopped):
            probe.get("/api/v1/candles", {})
        row = probe.observations[0]
        self.assertEqual(row["api_error_code"], "rate-limit-exceeded")
        self.assertEqual(row["request_id"], "01HXYZABCDEFG123456789")
        self.assertNotIn("PRIVATE", json.dumps(row))
        self.assertEqual(opener.open.call_count, 1)

    def test_fixed_probe_records_shapes_without_raw_data_or_headers(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: Response(
            {"result": {"rankedAt": None, "rankings": [{"symbol": "005930", "price": {"lastPrice": "123"}}]}},
            headers={"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "SYNTHETIC-TOKEN", "Authorization": "SYNTHETIC-TOKEN"})
        probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
        observations = sample_rest(probe, ["005930", "000660"])
        self.assertEqual(len(observations), 9)
        encoded = json.dumps(observations)
        self.assertNotIn("SYNTHETIC-TOKEN", encoded)
        self.assertNotIn('"123"', encoded)
        self.assertEqual(observations[0]["rate_headers"], {"X-RateLimit-Limit": "10"})
        urls = [c.args[0].full_url for c in opener.open.call_args_list]
        self.assertIn("duration=realtime", urls[0])
        self.assertTrue(all("adjusted=false" in u for u in urls if "/candles" in u))

    def test_invalid_issuer_and_sample_sizes_rejected(self):
        for values in ([], ["005930"] * 2, ["../key"], ["a"], ["005930"] * 6):
            with self.subTest(values=values), self.assertRaises(ProbeStopped):
                symbols_checked(values)

    def test_websocket_slot_unknown_never_connects(self):
        connect = Mock()
        with self.assertRaisesRegex(ProbeStopped, "SLOT_CONFIRMATION_REQUIRED"):
            sample_websocket("SYNTHETIC-TOKEN", ["005930"], connect=connect)
        connect.assert_not_called()

    def test_cli_error_never_prints_credentials_or_traceback(self):
        out = io.StringIO()
        with patch("shared_toss_auth.SharedTokenCache", side_effect=RuntimeError("SYNTHETIC-SECRET")), \
                patch("pathlib.Path.read_text", return_value="SYNTHETIC-CLIENT"), contextlib.redirect_stdout(out):
            code = main(["--client-id-file", "synthetic-id", "--cache-dir", "synthetic-cache"])
        self.assertEqual(code, 1)
        self.assertNotIn("SYNTHETIC-SECRET", out.getvalue())
        record = json.loads(out.getvalue())
        self.assertFalse(record["token_issued"])
        self.assertEqual(record["websocket"]["status"], "NOT_RUN_SLOT_UNCONFIRMED")

    def test_skip_candles_never_requests_candle_url(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: Response({"result": {"rankedAt": None, "rankings": []}})
        probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
        sample_rest(probe, ["005930", "000660"], skip_candles=True)
        self.assertEqual(opener.open.call_count, 7)
        self.assertTrue(all("/candles" not in call.args[0].full_url for call in opener.open.call_args_list))

    def test_cli_skip_candles_uses_fixture_auth_and_preserves_slot_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            client_file = root / "synthetic-client.txt"
            client_file.write_text("SYNTHETIC-CLIENT", encoding="utf-8")
            cache = SharedTokenCache(root / "synthetic-cache", "SYNTHETIC-CLIENT")
            cache.get(lambda: {"access_token": "SYNTHETIC-TOKEN", "expires_in": 600})
            opener = Mock()
            opener.open.side_effect = lambda *a, **kw: Response({"result": {"rankedAt": None, "rankings": []}})
            probe = PublicRestProbe("SYNTHETIC-TOKEN", opener=opener, pause=lambda _: None)
            out = io.StringIO()
            with patch("toss_intraday_probe.PublicRestProbe", return_value=probe), \
                    patch("toss_intraday_probe.sample_websocket") as websocket, contextlib.redirect_stdout(out):
                code = main(["--client-id-file", str(client_file), "--cache-dir", str(cache.directory), "--skip-candles"])
            self.assertEqual(code, 0)
            websocket.assert_not_called()
            self.assertTrue(all("/candles" not in call.args[0].full_url for call in opener.open.call_args_list))
            self.assertTrue(json.loads(out.getvalue())["cache_bytes_unchanged"])
            self.assertNotIn("SYNTHETIC-TOKEN", out.getvalue())

    def sample(self, server, *, reconnect=False):
        with patch.dict("sys.modules", {"websockets": None, "websockets.sync.client": None}):
            return sample_websocket("SYNTHETIC-TOKEN", ["005930", "000660"], slot_available=True,
                seconds=5, connect=server.connect, verify_reconnect=reconnect, clock=server.clock,
                wall_clock=lambda: datetime.fromisoformat("2026-11-13T10:00:01+09:00"))

    def test_injected_socket_measures_one_ping_pong_null_times_and_negative_age(self):
        def frame(channel, timestamp):
            return {"type": "message", "topic": channel + ":kr:005930", "data": {"currency": "KRW", "timestamp": timestamp}}
        server = ProbeSocketServer(frames=[frame("trade", "2026-11-13T10:00:00+09:00"),
            frame("orderbook", None), frame("orderbook", None), frame("trade", None),
            frame("trade", "2026-11-13T10:00:02+09:00")])
        result = self.sample(server)
        self.assertEqual(result["status"], "OBSERVED")
        self.assertTrue(result["pong_received"])
        self.assertGreater(result["pong_rtt_ms"], 0)
        self.assertEqual(result["null_timestamp_counts"], {"trade": 1, "orderbook": 2})
        self.assertEqual(result["negative_age_samples"], 1)
        self.assertEqual(result["event_age_ms"]["trade"]["samples"], 2)
        self.assertEqual(server.sockets[0].sent.count("PING"), 1)
        self.assertTrue(server.sockets[0].closed)
        self.assertEqual(result["library_version"], "INJECTED_SOCKET")
        self.assertNotIn("SYNTHETIC-TOKEN", json.dumps(result))

    def test_reconnect_is_one_ack_after_unsubscribe_and_completed_first_close(self):
        server = ProbeSocketServer()
        result = self.sample(server, reconnect=True)
        self.assertEqual(server.trace, [("connect", 1), ("unsubscribe", 1), ("close", 1),
            ("connect", 2), ("unsubscribe", 2), ("close", 2)])
        self.assertTrue(result["first_socket_closed"])
        self.assertEqual(result["reconnect"]["status"], "OBSERVED")
        self.assertEqual(result["reconnect"]["acknowledged_topics"], 4)
        self.assertEqual(sum(s.sent.count("PING") for s in server.sockets), 1)
        self.assertTrue(all(s.closed for s in server.sockets))

    def test_failed_first_close_prevents_any_reconnect(self):
        server = ProbeSocketServer(close_failure=True)
        with self.assertRaises(OSError): self.sample(server, reconnect=True)
        self.assertEqual(len(server.sockets), 1)
        self.assertEqual(server.trace[-1], ("close", 1))

    def test_unsubscribe_failure_still_closes_and_does_not_reconnect(self):
        server = ProbeSocketServer(unsubscribe_failure=True)
        with self.assertRaises(OSError): self.sample(server, reconnect=True)
        self.assertTrue(server.sockets[0].closed)
        self.assertEqual(len(server.sockets), 1)

    def test_missing_ack_or_pong_is_incomplete_and_does_not_open_second_socket(self):
        for server, status in [(ProbeSocketServer(drop_ack=[1]), "ACK_UNOBSERVED"),
                               (ProbeSocketServer(drop_pong=True), "PONG_UNOBSERVED")]:
            with self.subTest(status=status):
                result = self.sample(server, reconnect=True)
                self.assertEqual(result["status"], status)
                self.assertFalse(result["reconnect"]["attempted"])
                self.assertEqual(len(server.sockets), 1)
                self.assertTrue(server.sockets[0].closed)

    def test_second_ack_failure_closes_both_and_has_no_automatic_retry(self):
        server = ProbeSocketServer(reject_ack=[2])
        with self.assertRaisesRegex(ProbeStopped, "NOT_FULLY_ACCEPTED"):
            self.sample(server, reconnect=True)
        self.assertEqual(len(server.sockets), 2)
        self.assertTrue(all(s.closed for s in server.sockets))
        server = ProbeSocketServer(drop_ack=[2])
        result = self.sample(server, reconnect=True)
        self.assertEqual(result["status"], "RECONNECT_ACK_UNOBSERVED")
        self.assertEqual(len(server.sockets), 2)
        self.assertTrue(all(s.closed for s in server.sockets))

    def test_unapproved_topic_currency_and_partial_ack_are_rejected_with_close(self):
        for server in [ProbeSocketServer(reject_ack=[1]),
            ProbeSocketServer(frames=[{"type": "message", "topic": "personal:order:3", "data": {"currency": "KRW"}}]),
            ProbeSocketServer(frames=[{"type": "message", "topic": "trade:kr:005930", "data": {"currency": "USD"}}])]:
            with self.subTest(server=server), self.assertRaises(ProbeStopped):
                self.sample(server, reconnect=True)
            self.assertEqual(len(server.sockets), 1)
            self.assertTrue(server.sockets[0].closed)

    def test_verify_reconnect_option_does_not_authorize_a_connection_or_auth_read(self):
        out = io.StringIO()
        with patch("shared_toss_auth.SharedTokenCache") as cache, \
                patch("toss_intraday_probe.PublicRestProbe") as rest, contextlib.redirect_stdout(out):
            code = main(["--client-id-file", "unused", "--cache-dir", "unused", "--verify-reconnect", "--skip-candles"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["error_code"], "ACCOUNT_WEBSOCKET_SLOT_CONFIRMATION_REQUIRED")
        cache.assert_not_called()
        rest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
