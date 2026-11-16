import json
import random
import unittest
from unittest.mock import patch

from daytrade_harness import FakeServer
from stock_assistant.daytrade.market import QualityPolicy, MarketEvent
from stock_assistant.daytrade.observation import (websocket_event, rankings_event, runtime_sample,
                                                 observation_report)
from stock_assistant.daytrade.websocket import WebSocketAdapter

NOW = "2026-11-13T10:00:02+09:00"
QUALITY = QualityPolicy(5, 10, 120)  # Explicit synthetic thresholds, not runtime defaults.


def metadata(event_id, **kwargs):
    return {"event_id": event_id, "received_at": NOW, "received_monotonic": 2,
            "session_id": "SYNTHETIC", "quality": QUALITY, **kwargs}


def trade(code="005930", stamp="2026-11-13T10:00:00+09:00"):
    return {"type": "message", "topic": "trade:kr:" + code,
            "data": {"currency": "KRW", "timestamp": stamp, "price": "100", "volume": "5"}}


def book(code="005930", stamp="2026-11-13T10:00:00+09:00", bid="99", ask="100"):
    return {"type": "message", "topic": "orderbook:kr:" + code,
            "data": {"currency": "KRW", "timestamp": stamp,
                     "asks": [{"price": ask, "volume": "10"}], "bids": [{"price": bid, "volume": "10"}]}}


def ranking(stamp="2026-11-13T10:00:00+09:00"):
    return {"rankedAt": stamp, "rankings": [{"symbol": code, "currency": "KRW", "tradingAmount": "1000", "tradingVolume": "10"}
                                            for code in ("005930", "000660")]}


class ObservationTests(unittest.TestCase):
    def test_adapter_callback_normalizes_trade_without_library_or_network(self):
        events = []
        server = FakeServer()
        adapter = WebSocketAdapter(server.connect, rng=random.Random(1),
            on_message=lambda frame: events.append(websocket_event(frame, **metadata("ws-" + str(len(events) + 1)))))
        adapter.replace(["trade:kr:005930", "orderbook:kr:005930"], now=0)
        with patch.dict("sys.modules", {"websockets": None}):
            adapter.connect(now=0)
            adapter.poll(now=0)
            server.sockets[-1].push(trade())
            adapter.poll(now=1)
            server.sockets[-1].push(book())
            adapter.poll(now=2)
        adapter.close()
        event = events[0]
        self.assertIsInstance(event, MarketEvent)
        self.assertEqual((event.kind, event.symbol, event.source), ("trade", "005930", "TOSS_WEBSOCKET"))
        self.assertEqual(event.market, "INTEGRATED_KR")
        self.assertEqual(event.data["order_strength"], "UNAVAILABLE")
        self.assertEqual(event.data["tick_vwap"], "UNAVAILABLE")
        self.assertEqual((events[1].kind, events[1].symbol), ("orderbook", "005930"))

    def test_orderbook_bridge_preserves_null_empty_crossed_and_stale_issues(self):
        empty = book(stamp=None)
        empty["data"].update(asks=[], bids=[])
        cases = [(empty, {"SOURCE_TIMESTAMP_UNAVAILABLE", "EMPTY_ORDERBOOK"}),
                 (book(bid="100", ask="100"), {"CROSSED_ORDERBOOK"}),
                 (book(stamp="2026-11-13T09:59:00+09:00"), {"STALE_ORDERBOOK"})]
        for frame, expected in cases:
            with self.subTest(expected=expected):
                event = websocket_event(frame, **metadata("book"))
                self.assertTrue(expected <= set(event.issues))
                self.assertTrue(event.entry_blocked)

    def test_rankings_bridge_accepts_envelope_or_result_without_changing_contract(self):
        one = rankings_event(ranking(), **metadata("rank"))
        two = rankings_event({"result": ranking()}, **metadata("rank"))
        self.assertEqual(one.fingerprint, two.fingerprint)
        self.assertEqual((one.kind, one.symbol, one.source), ("rankings", "ALL", "TOSS_REST_RANKINGS"))
        self.assertEqual([r["symbol"] for r in one.data["rankings"]], ["005930", "000660"])
        delayed = rankings_event(ranking("2026-11-13T09:59:00+09:00"), **metadata("late"))
        self.assertIn("STALE_RANKINGS", delayed.issues)

    def test_bridge_rejects_private_foreign_mismatched_and_future_inputs(self):
        variants = [{"type": "subscriptions"}, {"type": "message", "topic": "personal:order:3", "data": {}},
                    {"type": "message", "topic": "trade:us:AAPL", "data": {}}]
        wrong = trade(); wrong["data"]["symbol"] = "000660"; variants.append(wrong)
        wrong = trade(); wrong["data"]["currency"] = "USD"; variants.append(wrong)
        variants.append(trade(stamp="2099-01-01T00:00:00Z"))
        for frame in variants:
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                websocket_event(frame, **metadata("bad"))
        with self.assertRaises(ValueError):
            rankings_event({"error": {"message": "PRIVATE"}}, **metadata("bad-rest"))

    def samples(self):
        empty = book(stamp=None); empty["data"].update(asks=[], bids=[])
        events = [websocket_event(trade(), **metadata("good")),
                  websocket_event(empty, **metadata("missing")),
                  websocket_event(book("000660", bid="101", ask="100"), **metadata("crossed")),
                  websocket_event(book("000660", stamp="2026-11-13T09:59:00+09:00"), **metadata("stale")),
                  rankings_event(ranking("2026-11-13T09:59:00+09:00"), **metadata("ranking"))]
        runtime = [runtime_sample({"status": "RATE_LIMIT_DEGRADED", "group": "MARKET_DATA_CHART", "entry_blocked": True},
                                  event_id="rate", received_at=NOW),
                   runtime_sample({"status": "OK", "group": "MARKET_DATA", "entry_blocked": False},
                                  event_id="ok", received_at=NOW, issuer="005930")]
        return events, runtime

    def test_daily_report_counts_quality_runtime_and_per_issuer_memberships(self):
        events, runtime = self.samples()
        report = observation_report(events, day="2026-11-13", runtime_samples=runtime)
        self.assertEqual(report["observations"], 7)
        self.assertEqual(report["market"]["observations"], 5)
        self.assertEqual(report["runtime"]["observations"], 2)
        self.assertEqual(report["entry_blocked"], {"count": 5, "total": 7, "ratio": 5 / 7})
        self.assertEqual(report["issue_counts"]["SOURCE_TIMESTAMP_UNAVAILABLE"], 1)
        self.assertEqual(report["issue_counts"]["CROSSED_ORDERBOOK"], 1)
        self.assertEqual(report["issue_counts"]["EMPTY_ORDERBOOK"], 1)
        self.assertEqual(report["issue_counts"]["RATE_LIMIT_DEGRADED"], 1)
        self.assertEqual(report["stale_total"], 2)
        self.assertEqual(report["per_symbol"]["005930"]["observations"], 4)
        self.assertEqual(report["per_symbol"]["005930"]["entry_blocked"]["ratio"], .5)
        self.assertEqual(report["per_symbol"]["000660"]["observations"], 3)
        self.assertEqual(report["per_symbol"]["000660"]["entry_blocked"]["ratio"], 1)
        self.assertEqual(report["mode"], "OBSERVE_ONLY")
        self.assertFalse(report["paper_ready"])
        self.assertFalse(report["execution_enabled"])
        json.dumps(report)

    def test_report_is_order_independent_and_deduplicates_only_identical_ids(self):
        events, runtime = self.samples()
        a = observation_report(events, day="2026-11-13", runtime_samples=runtime)
        self.assertEqual(a, observation_report(list(reversed(events)), day="2026-11-13", runtime_samples=list(reversed(runtime))))
        duplicate = observation_report(events + [events[0]], day="2026-11-13", runtime_samples=runtime)
        self.assertEqual(duplicate["observations"], a["observations"])
        self.assertEqual(duplicate["duplicates_ignored"], 1)
        repeated_tick = websocket_event(trade(), **metadata("another-physical-or-repeated-frame"))
        self.assertEqual(observation_report([events[0], repeated_tick], day="2026-11-13")["observations"], 2)

    def test_conflicting_ids_are_rejected_instead_of_silently_merged(self):
        one = websocket_event(trade(), **metadata("same"))
        two = websocket_event(book(), **metadata("same"))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            observation_report([one, two], day="2026-11-13")

    def test_kst_day_filter_uses_receive_day_not_source_day_or_utc_day(self):
        event = websocket_event(trade(stamp="2026-11-12T15:59:59Z"),
            **metadata("kst-day", received_at="2026-11-12T16:00:00Z"))
        self.assertEqual(observation_report([event], day="2026-11-13")["observations"], 1)
        prior_day = observation_report([event], day="2026-11-12")
        self.assertEqual(prior_day["observations"], 0)
        self.assertEqual(prior_day["excluded_other_days"], 1)

    def test_empty_report_has_unknown_ratio_and_no_invented_policy(self):
        report = observation_report([], day="2026-11-13")
        self.assertIsNone(report["entry_blocked"]["ratio"])
        self.assertEqual(report["issue_counts"]["RATE_LIMIT_DEGRADED"], 0)
        event = websocket_event(trade(), **metadata("unset", quality=QualityPolicy()))
        report = observation_report([event], day="2026-11-13")
        self.assertEqual(report["entry_blocked"]["ratio"], 1)
        self.assertIn("FRESHNESS_POLICY_UNSET", report["issue_counts"])
        self.assertEqual(QualityPolicy(), QualityPolicy(None, None, None))

    def test_runtime_samples_reject_invalid_flags_and_do_not_retain_payload(self):
        result = {"status": "RATE_LIMIT_DEGRADED", "group": "RANKINGS", "entry_blocked": True,
                  "payload": {"secret": "SYNTHETIC-SECRET"}}
        sample = runtime_sample(result, event_id="rate", received_at=NOW)
        report = observation_report([], day="2026-11-13", runtime_samples=[sample])
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(report))
        for changed in ({"entry_blocked": 1}, {"entry_blocked": False}, {"group": "ACCOUNT"}, {"status": "PAPER_READY"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                runtime_sample({**result, **changed}, event_id="bad", received_at=NOW)

    def test_bridges_and_summary_have_no_database_or_network_side_effects(self):
        with patch("sqlite3.connect", side_effect=AssertionError("no database")), \
                patch("socket.create_connection", side_effect=AssertionError("no network")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            events, runtime = self.samples()
            report = observation_report(events, day="2026-11-13", runtime_samples=runtime)
        self.assertEqual(report["observations"], 7)
