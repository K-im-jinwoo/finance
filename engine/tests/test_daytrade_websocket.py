import json
import random
import unittest
from unittest.mock import Mock, patch
from daytrade_harness import FakeServer
from stock_assistant.daytrade.websocket import WebSocketAdapter, runtime_socket_factory


class WebSocketTests(unittest.TestCase):
    def make(self, rejected=()):
        server = FakeServer(rejected)
        client = WebSocketAdapter(server.connect, rng=random.Random(8))
        client.replace(["trade:kr:005930", "orderbook:kr:005930"], now=0)
        client.connect(now=0)
        client.poll(now=0)
        return server, client

    def test_full_replace_partial_rejected_and_data_scope_without_optional_library(self):
        with patch.dict("sys.modules", {"websockets": None}):
            server, client = self.make(["orderbook:kr:005930"])
            self.assertEqual(client.active, {"trade:kr:005930"})
            self.assertTrue(client.entry_blocked)
            sock = server.sockets[-1]
            sock.push({"type": "message", "topic": "trade:kr:005930", "data": {"currency": "KRW"}})
            client.poll(now=1)
            self.assertEqual(len(client.messages), 1)
            client.replace(["trade:kr:000660"], now=2)
            client.poll(now=2)
            self.assertEqual(sock.topics, {"trade:kr:000660"})
            self.assertFalse(client.entry_blocked)
            client.replace([], now=3)
            client.poll(now=3)
            self.assertEqual(sock.sent[-1], "[]")
            self.assertFalse(client.active)

    def test_ping_exactly_at_60_seconds_and_pong_clears_wait(self):
        server, c = self.make()
        sock = server.sockets[-1]
        c.tick(now=59.999)
        self.assertNotIn("PING", sock.sent)
        c.tick(now=60)
        self.assertEqual(sock.sent[-1], "PING")
        c.poll(now=60)
        self.assertIsNone(c.waiting_pong)
        c.tick(now=119)
        self.assertEqual(sock.sent.count("PING"), 1)

    def test_server_shutdown_closes_before_backoff_reconnect_and_resubscribes(self):
        server, c = self.make()
        server.sockets[-1].push({"type": "error", "error": {"code": "server-shutdown"}})
        c.poll(now=1)
        self.assertEqual(server.trace, ["connect", "close"])
        c.tick(now=c.retry_at)
        c.poll(now=c.last_now)
        self.assertEqual(server.trace, ["connect", "close", "connect"])
        self.assertFalse(c.entry_blocked)
        self.assertEqual(len(c.active), 2)

    def test_auth_revocation_waits_for_explicit_auth_owner_and_never_overlaps(self):
        server, c = self.make()
        server.sockets[-1].push({"type": "error", "error": {"code": "auth-revoked"}})
        c.poll(now=1)
        c.tick(now=1000)
        self.assertEqual(server.trace, ["connect", "close"])
        with self.assertRaises(ValueError): c.auth_restored(now=1000)
        c.auth_restored(verified=True, now=1000)
        c.poll(now=1000)
        self.assertEqual(server.trace, ["connect", "close", "connect"])

    def test_missing_pong_reconnects_and_private_topics_are_never_sent(self):
        server, c = self.make()
        c.tick(now=60)
        server.sockets[-1].frames.clear()
        c.tick(now=90)
        self.assertEqual(c.status, "BACKOFF")
        with self.assertRaises(ValueError): c.replace(["personal:order:3"], now=90)

    def test_unacknowledged_or_foreign_currency_frame_degrades_and_closes(self):
        for frame in ({"type": "message", "topic": "trade:kr:000660", "data": {"currency": "KRW"}},
                      {"type": "message", "topic": "trade:kr:005930", "data": {"currency": "USD"}},
                      {"type": "subscriptions", "subscribed": [], "rejected": []}):
            server, c = self.make()
            server.sockets[-1].push(frame)
            c.poll(now=1)
            self.assertTrue(c.entry_blocked)
            self.assertTrue(server.sockets[-1].closed)

    def test_explicit_reconnect_also_closes_old_connection_first(self):
        server, c = self.make()
        c.connect(now=2)
        self.assertEqual(server.trace, ["connect", "close", "connect"])

    def test_slot_guard_precedes_library_import_and_token_access(self):
        factory = runtime_socket_factory(lambda: self.fail("token must not be read"))
        with patch.dict("sys.modules", {"websockets": None}), self.assertRaisesRegex(ValueError, "slot confirmation"):
            factory()

    def test_close_failure_never_opens_an_overlapping_connection(self):
        server, c = self.make()
        def fail(): raise OSError("synthetic close failed")
        server.sockets[-1].close = fail
        with self.assertRaises(OSError): c.connect(now=1)
        self.assertEqual(c.status, "CLOSE_FAILED")
        self.assertTrue(c.entry_blocked)
        self.assertEqual(server.trace, ["connect"])

    def test_declaration_rate_limit_is_checked_locally_without_sending_sixth(self):
        server, c = self.make()
        for _ in range(4):
            c.replace(["trade:kr:005930"], now=0)
            c.poll(now=0)
        with self.assertRaisesRegex(ValueError, "five declarations"):
            c.replace(["trade:kr:000660"], now=0)
        self.assertEqual(len(server.sockets[-1].sent), 5)

    def test_active_inflight_frame_is_allowed_until_replacement_ack_arrives(self):
        server, c = self.make()
        sock = server.sockets[-1]
        c.replace(["trade:kr:000660"], now=1)
        pending = c.pending
        # The server has queued the new ack; an older authorized frame arrives first.
        old_frame = {"type": "message", "topic": "trade:kr:005930", "data": {"currency": "KRW"}}
        sock.frames.appendleft(json.dumps(old_frame))
        c.poll(now=1)
        self.assertFalse(sock.closed)
        self.assertEqual(c.pending, pending)
        self.assertEqual(list(c.messages), [old_frame])
        self.assertEqual(c.status, "AWAITING_ACK")
        self.assertTrue(c.entry_blocked)
        c.poll(now=2)
        self.assertEqual(c.active, {"trade:kr:000660"})
        self.assertEqual(c.status, "LIVE")
        # After the ack, the removed topic is no longer authorized.
        sock.push(old_frame)
        c.poll(now=3)
        self.assertEqual(c.status, "DATA_DEGRADED")
        self.assertTrue(sock.closed)

    def test_first_connection_and_unapproved_pending_topics_remain_blocked(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                if initial:
                    server = FakeServer()
                    c = WebSocketAdapter(server.connect, rng=random.Random(8))
                    c.replace(["trade:kr:005930"], now=0)
                    c.connect(now=0)
                    topic = "trade:kr:005930"
                else:
                    server, c = self.make()
                    c.replace(["trade:kr:000660"], now=0)
                    topic = "trade:kr:000660"
                sock = server.sockets[-1]
                sock.frames.appendleft(json.dumps({"type": "message", "topic": topic, "data": {"currency": "KRW"}}))
                c.poll(now=0)
                self.assertTrue(sock.closed)
                self.assertEqual(c.status, "DATA_DEGRADED")
                self.assertFalse(c.messages)

    def test_ping_send_failure_schedules_backoff_and_closes_before_reconnect(self):
        for error_type in (OSError, ConnectionError):
            with self.subTest(error=error_type):
                server, c = self.make()
                old = server.sockets[-1]
                old.send = Mock(side_effect=error_type("synthetic send failure"))
                c.tick(now=60)
                self.assertEqual(c.status, "BACKOFF")
                self.assertEqual(c.failures, 1)
                self.assertIsNone(c.pending)
                self.assertIsNone(c.waiting_pong)
                self.assertTrue(c.entry_blocked)
                self.assertTrue(old.closed)
                self.assertGreater(c.retry_at, 60)
                c.tick(now=c.retry_at)
                c.poll(now=c.last_now)
                self.assertEqual(server.trace, ["connect", "close", "connect"])
                self.assertEqual(c.status, "LIVE")

    def test_declaration_send_failure_has_same_recovery_for_connect_and_replace(self):
        for initial in (True, False):
            for error_type in (OSError, ConnectionError):
                with self.subTest(initial=initial, error=error_type):
                    if initial:
                        server = FakeServer()
                        first = [True]
                        def factory():
                            sock = server.connect()
                            if first[0]:
                                first[0] = False
                                sock.send = Mock(side_effect=error_type("synthetic send failure"))
                            return sock
                        c = WebSocketAdapter(factory, rng=random.Random(8))
                        c.replace(["trade:kr:000660"], now=0)
                        c.connect(now=0)
                    else:
                        server, c = self.make()
                        server.sockets[-1].send = Mock(side_effect=error_type("synthetic send failure"))
                        c.replace(["trade:kr:000660"], now=0)
                    self.assertEqual(c.status, "BACKOFF")
                    self.assertEqual(c.failures, 1)
                    self.assertTrue(server.sockets[-1].closed)
                    self.assertIsNone(c.pending)
                    self.assertFalse(c.active)
                    self.assertGreater(c.retry_at, 0)
                    c.tick(now=c.retry_at)
                    c.poll(now=c.last_now)
                    self.assertEqual(server.trace, ["connect", "close", "connect"])
                    self.assertEqual(c.active, {"trade:kr:000660"})
