import unittest
from decimal import Decimal
from stock_assistant.daytrade.market import QualityPolicy, normalize, bar_typical_vwap_approx

NOW = "2026-11-13T10:02:30+09:00"
QUALITY = QualityPolicy(5, 10, 120)


def event(kind, payload, **kw):
    return normalize(kind, payload, event_id="sample", received_at=NOW, received_monotonic=10,
                     session_id="synthetic", quality=kw.pop("quality", QUALITY),
                     issuer="ALL" if kind == "rankings" else "005930", **kw)


def book(timestamp="2026-11-13T10:02:29+09:00"):
    return {"currency": "KRW", "timestamp": timestamp, "asks": [{"price": "101", "volume": "100"}],
            "bids": [{"price": "100", "volume": "100"}]}


def bar(at, volume="3"):
    return {"currency": "KRW", "timestamp": at, "openPrice": "100", "highPrice": "104",
            "lowPrice": "98", "closePrice": "101", "volume": volume}


class MarketTests(unittest.TestCase):
    def test_good_book_is_normalized_and_payload_mutation_cannot_change_hash(self):
        e = event("orderbook", book())
        self.assertFalse(e.entry_blocked)
        before = e.fingerprint
        copy = e.data
        copy["asks"][0]["price"] = "999"
        self.assertEqual(e.fingerprint, before)
        self.assertEqual(e.market, "INTEGRATED_KR")

    def test_null_empty_locked_crossed_and_stale_quotes_block_entry(self):
        variants = [book(None), book("2026-11-13T09:00:00+09:00")]
        b = book(); b["asks"] = []; variants.append(b)
        for bid in ("101", "102"):
            b = book(); b["bids"][0]["price"] = bid; variants.append(b)
        for b in variants:
            with self.subTest(b=b):
                self.assertTrue(event("orderbook", b).entry_blocked)
        self.assertTrue(event("orderbook", book(), quality=QualityPolicy()).entry_blocked)

    def test_currency_future_timestamp_and_invalid_numbers_are_rejected(self):
        b = book(); b["currency"] = "USD"
        with self.assertRaises(ValueError): event("orderbook", b)
        with self.assertRaises(ValueError): event("orderbook", book("2099-01-01T00:00:00Z"))
        b = book(); b["asks"][0]["volume"] = "NaN"
        with self.assertRaises(ValueError): event("orderbook", b)

    def test_delayed_rankings_block_even_when_http_was_fast(self):
        r = {"rankedAt": "2026-11-13T10:02:09+09:00", "rankings": [{"currency": "KRW", "symbol": "005930", "tradingAmount": "100", "tradingVolume": "10"}]}
        self.assertIn("STALE_RANKINGS", event("rankings", r).issues)

    def test_candles_are_ascending_end_times_and_future_bar_is_not_used(self):
        e = event("candles", {"candles": [bar("2026-11-13T10:03:00+09:00"), bar("2026-11-13T10:02:00+09:00"), bar("2026-11-13T10:01:00+09:00")]})
        self.assertEqual(len(e.data["candles"]), 2)
        self.assertLess(e.data["candles"][0]["timestamp"], e.data["candles"][1]["timestamp"])
        self.assertEqual(e.data["incomplete_dropped"], 1)
        self.assertEqual(bar_typical_vwap_approx(e)["value"], Decimal("101"))
        self.assertEqual(e.data["tick_vwap"], "UNAVAILABLE")

    def test_zero_volume_and_conflicting_candles(self):
        b = bar("2026-11-13T10:02:00+09:00", "0")
        self.assertEqual(bar_typical_vwap_approx(event("candles", {"candles": [b]}))["status"], "UNAVAILABLE")
        other = dict(b, volume="1")
        with self.assertRaises(ValueError): event("candles", {"candles": [b, other]})

    def test_daily_source_is_separate_and_current_day_close_is_forbidden(self):
        p = {"currency": "KRW", "trade_date": "2026-11-12", "close": "100", "volume": "20"}
        self.assertEqual(event("daily", p).market, "KRX_DAILY")
        self.assertEqual(event("daily", p).source, "KRX_OPENAPI")
        p["trade_date"] = "2026-11-13"
        with self.assertRaises(ValueError): event("daily", p)

    def test_trade_does_not_invent_buy_sell_strength_or_tape_vwap(self):
        p = {"currency": "KRW", "timestamp": "2026-11-13T10:02:29+09:00", "price": "100", "volume": "3"}
        e = event("trade", p)
        self.assertEqual((e.data["order_strength"], e.data["tick_vwap"]), ("UNAVAILABLE", "UNAVAILABLE"))
