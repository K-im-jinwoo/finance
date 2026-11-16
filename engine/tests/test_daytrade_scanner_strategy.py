"""Synthetic thresholds below are test assumptions, never trading policy."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import unittest

from stock_assistant.daytrade import (MODE, POLICY_VERSION, ScannerConfig, ScanResult,
    StrategyConfig, StrategyEngine, StrategySignal, scan_rankings)
from stock_assistant.daytrade.contracts import instant
from stock_assistant.daytrade.market import QualityPolicy, normalize
from stock_assistant.daytrade.risk import RiskState

NOW = "2026-11-13T10:04:01+09:00"
CODE = "005930"
QUALITY = QualityPolicy(5, 30, 90)


def observation(kind, payload, *, issuer=CODE, at=NOW, event_id=None, quality=QUALITY):
    return normalize(kind, payload, event_id=event_id or kind, received_at=at,
        received_monotonic=10, session_id="synthetic", quality=quality,
        issuer="ALL" if kind == "rankings" else issuer)


def quote(*, issuer=CODE, at=NOW, stamp=None, bid="109", ask="110", volume="1000", event_id=None):
    return observation("orderbook", {"currency": "KRW", "timestamp": stamp or at,
        "bids": [{"price": bid, "volume": volume}], "asks": [{"price": ask, "volume": "1000"}]},
        issuer=issuer, at=at, event_id=event_id)


def bar(minute, close="100", volume="100", day="2026-11-13"):
    return {"currency": "KRW", "timestamp": f"{day}T10:{minute:02}:00+09:00",
        "openPrice": close, "highPrice": close, "lowPrice": close,
        "closePrice": close, "volume": volume}


def candle_event(*, closes=("100", "100", "100", "110"), volumes=("100", "100", "100", "400"),
                 at=NOW, event_id="candles"):
    return observation("candles", {"candles": [bar(i, p, v) for i, (p, v) in
        enumerate(zip(closes, volumes), 1)]}, at=at, event_id=event_id)


def scanner_config(**kw):
    return ScannerConfig(**dict(min_price="50", min_turnover="1000000", max_spread_bps="100",
        quote_max_age_seconds=5, ranking_max_age_seconds=30, **kw))


def strategy_config(**kw):
    values = dict(min_candles=4, volume_spike_ratio="2", min_bid_volume="500",
        max_spread_bps="100", quote_max_age_seconds=5, candle_max_age_seconds=90,
        take_profit_bps="500", stop_loss_bps="300")
    return StrategyConfig(**(values | kw))


def ranking(rows, *, at=NOW):
    return {"scope": "INTEGRATED_KR", "rankedAt": at, "rankings": [
        {"rank": i, "symbol": code, "name": "Synthetic " + code, "currency": "KRW",
         "price": {"lastPrice": price}, "tradingAmount": amount, "tradingVolume": "10000"}
        for i, (code, price, amount) in enumerate(rows, 1)]}


def risk(at=NOW):
    result = RiskState()
    result.advance(at, opening_equity="10000000")
    result.calendar_verified = True
    return result


def ready_engine(*, config=None, book=None, candles=None):
    result = StrategyEngine(config or strategy_config())
    result.on_event(candles or candle_event())
    result.on_event(book or quote())
    return result


class ScannerTests(unittest.TestCase):
    def test_union_liquidity_sort_rank_and_watch_limit(self):
        first = ranking([(CODE, "110", "2000000"), ("000660", "110", "3000000")])
        second = ranking([("000660", "110", "3000000"), ("035420", "110", "4000000")])
        books = {code: quote(issuer=code) for code in (CODE, "000660", "035420")}
        result = scan_rankings(first, second, config=scanner_config(max_watch=2), now=NOW, quotes=books)
        self.assertIsInstance(result, ScanResult)
        self.assertEqual([r["symbol"] for r in result.selected], ["035420", "000660"])
        self.assertEqual(result.selected[0]["rank_volume"], 2)
        self.assertIsNone(result.selected[0]["rank_turnover"])
        self.assertEqual(result.selected[1]["name"], "Synthetic 000660")
        self.assertEqual(result.selected[1]["scope"], "INTEGRATED_KR")
        self.assertEqual(result.selected[1]["spread_bps"], Decimal(20000) / 219)
        self.assertEqual(result.rejected[0]["reason"], "WATCH_LIMIT")

    def test_risk_low_liquidity_price_and_wide_spread_exclusions(self):
        rows = [(CODE, "110", "2000000"), ("000660", "110", "999999"),
                ("035420", "49", "2000000"), ("035720", "110", "2000000")]
        result = scan_rankings(ranking(rows), config=scanner_config(), now=NOW,
            risk_meta={CODE: {"halted": True}}, quotes={"035720": quote(issuer="035720", bid="100", ask="110")})
        self.assertFalse(result.selected)
        self.assertEqual({r["symbol"]: r["reason"] for r in result.rejected},
            {CODE: "HALTED", "000660": "LOW_TURNOVER", "035420": "LOW_PRICE", "035720": "WIDE_SPREAD"})

    def test_every_adverse_flag_and_invalid_metadata(self):
        for flag in ("delisting", "warning_or_caution", "major_negative_disclosure"):
            with self.subTest(flag=flag):
                result = scan_rankings(ranking([(CODE, "110", "2000000")]), now=NOW,
                    config=scanner_config(), risk_meta={CODE: {flag: True}})
                self.assertEqual(result.rejected[0]["reason"], flag.upper())
        with self.assertRaises(ValueError):
            scan_rankings(ranking([(CODE, "110", "2000000")]), now=NOW,
                config=scanner_config(), risk_meta={CODE: {"halted": "false"}})

    def test_missing_quote_is_watch_only_and_missing_metadata_is_disclosed(self):
        result = scan_rankings(ranking([(CODE, "110", "2000000")]), config=scanner_config(), now=NOW)
        selected = result.selected[0]
        self.assertTrue(selected["entry_blocked"])
        self.assertIsNone(selected["spread_bps"])
        self.assertIn("QUOTE_UNAVAILABLE_WATCH_ONLY", selected["selection_reasons"])
        self.assertIn("RISK_METADATA_UNAVAILABLE", selected["selection_reasons"])

    def test_normalized_rankings_use_quote_price_and_preserve_source_age(self):
        raw = ranking([(CODE, "110", "2000000")])
        event = observation("rankings", raw)
        good = scan_rankings(event, config=scanner_config(), now=NOW, quotes={CODE: quote()})
        self.assertEqual(good.selected[0]["turnover"], Decimal("2000000"))
        stale = scan_rankings(event, config=scanner_config(), now="2026-11-13T10:04:32+09:00")
        self.assertIn("STALE_RANKINGS", stale.rejected[0]["reasons"])

    def test_supplied_bad_quote_and_missing_timestamp_are_rejected(self):
        for event in (quote(stamp="2026-11-13T10:03:55+09:00"), quote(bid="110"),
                      quote(volume="0"), replace(quote(), source_timestamp=None)):
            with self.subTest(issues=event.issues):
                result = scan_rankings(ranking([(CODE, "110", "2000000")]),
                    config=scanner_config(), now=NOW, quotes={CODE: event})
                self.assertFalse(result.selected)
                self.assertTrue(result.rejected[0]["reasons"])

    def test_scope_currency_duplicate_rank_and_bad_config_are_rejected(self):
        raw = ranking([(CODE, "110", "2000000")])
        for bad in (raw | {"scope": "KRX"}, raw | {"rankings": [raw["rankings"][0] | {"currency": "USD"}]},
                    raw | {"rankings": raw["rankings"] * 2}):
            with self.assertRaises(ValueError):
                scan_rankings(bad, now=NOW, config=scanner_config())
        for values in ({"min_price": "NaN"}, {"max_watch": 0}, {"quote_max_age_seconds": True}):
            base = dict(min_price="50", min_turnover="1000000", max_spread_bps="100",
                        quote_max_age_seconds=5, ranking_max_age_seconds=30)
            with self.assertRaises(ValueError):
                ScannerConfig(**(base | values))

    def test_equal_liquidity_order_is_repeatable_and_thresholds_are_inclusive(self):
        raw = ranking([(CODE, "50", "1000000"), ("000660", "50", "1000000")])
        a = scan_rankings(raw, config=scanner_config(), now=NOW)
        b = scan_rankings(raw, config=scanner_config(), now=NOW)
        self.assertEqual(a, b)
        self.assertEqual([r["symbol"] for r in a.selected], [CODE, "000660"])

    def test_newest_quote_price_overrides_older_ranking_and_inputs_are_validated(self):
        raw = ranking([(CODE, "110", "1000000")])
        result = scan_rankings(raw, config=scanner_config(), now=NOW,
            quotes={CODE: quote(bid="40", ask="40.1")})
        self.assertEqual(result.rejected[0]["reason"], "LOW_PRICE")
        for values in ({"risk_meta": []}, {"quotes": []}):
            with self.assertRaises(ValueError):
                scan_rankings(raw, config=scanner_config(), now=NOW, **values)


class StrategyTests(unittest.TestCase):
    def test_scan_buy_vwap_breakout_volume_spike_and_take_profit(self):
        selected = scan_rankings(ranking([(CODE, "110", "2000000")]), now=NOW,
            config=scanner_config(), quotes={CODE: quote()}).selected[0]
        engine = ready_engine()
        signal = engine.evaluate(CODE, NOW, risk(), {}, selected)
        self.assertIsInstance(signal, StrategySignal)
        self.assertEqual((signal.action, signal.score, signal.reference_price), ("BUY", 100, Decimal("110")))
        self.assertEqual(signal.indicators["bar_typical_vwap_approx"], Decimal(740) / 7)
        self.assertEqual(signal.indicators["volume_spike_ratio"], Decimal(4))
        self.assertEqual(signal.indicators["tick_vwap"], "UNAVAILABLE")
        self.assertEqual(signal.indicators["order_strength"], "UNAVAILABLE")
        engine.on_event(quote(at="2026-11-13T10:04:02+09:00", bid="116", ask="117", event_id="profit-book"))
        sold = engine.evaluate(CODE, "2026-11-13T10:04:02+09:00", risk(),
            {CODE: {"quantity": 10, "average_price": "110"}})
        self.assertEqual((sold.action, sold.exit_reason, sold.reference_price), ("SELL", "TAKE_PROFIT", Decimal(116)))

    def test_quality_missing_stale_wide_crossed_and_unset_policy_hold(self):
        variants = [quote(stamp="2026-11-13T10:03:55+09:00"), quote(bid="100", ask="110"),
                    quote(bid="110"), quote(volume="0"), replace(quote(), source_timestamp=None),
                    replace(quote(), issues=("FRESHNESS_POLICY_UNSET",))]
        for book in variants:
            with self.subTest(book=book):
                self.assertEqual(ready_engine(book=book).evaluate(CODE, NOW, risk(), {}).action, "HOLD")
        engine = StrategyEngine(strategy_config())
        engine.on_event(candle_event())
        self.assertIn("ORDERBOOK_UNAVAILABLE", engine.evaluate(CODE, NOW, risk(), {}).reasons)

    def test_entry_cutoff_does_not_depend_on_caller_advancing_risk(self):
        engine = ready_engine()
        for at in ("2026-11-13T14:50:00+09:00", "2026-11-13T08:59:59+09:00"):
            with self.subTest(at=at):
                self.assertIn("ENTRY_NOT_ALLOWED", engine.evaluate(CODE, at, risk(), {}).reasons)
        self.assertEqual(engine.evaluate(CODE, NOW, risk("2026-11-13T14:50:00+09:00"), {}).action, "HOLD")

    def test_time_exit_without_fresh_book_and_loss_exit_priority(self):
        engine = ready_engine()
        positions = {CODE: {"quantity": 10, "average_price": "110"}}
        at = "2026-11-13T15:15:00+09:00"
        exited = engine.evaluate(CODE, at, risk(at), positions)
        self.assertEqual((exited.action, exited.exit_reason, exited.reference_price), ("SELL", "TIME_EXIT", None))
        r = risk()
        r.lock("LOSS_LOCKED", "FIXTURE")
        lost = engine.evaluate(CODE, NOW, r, positions)
        self.assertEqual(lost.exit_reason, "LOSS_EXIT")
        self.assertEqual(engine.evaluate(CODE, at, r, positions).exit_reason, "TIME_EXIT")

    def test_stop_loss_uses_bid_and_is_not_suppressed_by_entry_lock(self):
        engine = ready_engine(book=quote(bid="100", ask="101"))
        r = risk()
        r.lock("DATA_DEGRADED", "RANKINGS")
        result = engine.evaluate(CODE, NOW, r, {CODE: {"quantity": 10, "average_price": "110"}})
        self.assertEqual((result.action, result.exit_reason), ("SELL", "STOP_LOSS"))
        self.assertEqual(result.reference_price, Decimal(100))

    def test_profit_and_stop_thresholds_inclusive_and_stale_quote_cannot_trigger_price_exit(self):
        for bid, expected in (("105", "TAKE_PROFIT"), ("97", "STOP_LOSS")):
            engine = ready_engine(book=quote(bid=bid, ask=str(Decimal(bid) + Decimal("0.1"))))
            result = engine.evaluate(CODE, NOW, risk(), {CODE: {"quantity": 1, "average_price": "100"}})
            self.assertEqual(result.exit_reason, expected)
        engine = ready_engine(book=quote(bid="90", ask="90.1", stamp="2026-11-13T10:03:55+09:00"))
        result = engine.evaluate(CODE, NOW, risk(), {CODE: {"quantity": 1, "average_price": "100"}})
        self.assertEqual((result.action, result.reference_price), ("HOLD", None))

    def test_candle_freshness_insufficient_history_and_breakout_are_required(self):
        short = observation("candles", {"candles": [bar(3)]})
        self.assertIn("INSUFFICIENT_CANDLES", ready_engine(candles=short).evaluate(CODE, NOW, risk(), {}).reasons)
        engine = ready_engine(config=strategy_config(candle_max_age_seconds=0))
        self.assertIn("STALE_CANDLES", engine.evaluate(CODE, NOW, risk(), {}).reasons)
        candles = candle_event(closes=("100", "102", "104", "110"))
        result = ready_engine(candles=candles).evaluate(CODE, NOW, risk(), {})
        self.assertEqual(result.action, "HOLD")
        self.assertIn("MISSING_VWAP_BREAKOUT", result.reasons)

    def test_each_risk_lock_calendar_and_candidate_issue_blocks_entry(self):
        engine = ready_engine()
        for lock in ("LOSS_LOCKED", "RATE_LIMIT_DEGRADED", "DATA_DEGRADED", "PRIOR_DAY_UNFLATTENED"):
            r = risk()
            r.lock(lock, "FIXTURE")
            self.assertIn("ENTRY_NOT_ALLOWED", engine.evaluate(CODE, NOW, r, {}).reasons)
        r = risk(); r.calendar_verified = False
        self.assertEqual(engine.evaluate(CODE, NOW, r, {}).action, "HOLD")
        for meta in ({"halted": True}, {"delisting": True}, {"warning_or_caution": True},
                     {"major_negative_disclosure": True}, {"entry_blocked": True}, {"scope": "KRX"},
                     {"issues": ["STALE_RANKINGS"]}, {"symbol": "000660"},
                     {"valid_until": "2026-11-13T10:04:00+09:00"}):
            with self.subTest(meta=meta):
                self.assertEqual(engine.evaluate(CODE, NOW, risk(), {}, meta).action, "HOLD")

    def test_volume_spike_is_previous_median_and_bid_depth_is_required(self):
        for candles, book, reason in ((candle_event(volumes=("100", "100", "100", "199")), quote(), "MISSING_VOLUME_SPIKE"),
                                     (candle_event(), quote(volume="499"), "MISSING_BID_DEPTH")):
            result = ready_engine(candles=candles, book=book).evaluate(CODE, NOW, risk(), {})
            self.assertEqual(result.action, "HOLD")
            self.assertIn(reason, result.reasons)
            self.assertTrue(0 <= result.score < 100)
        self.assertEqual(ready_engine(candles=candle_event(volumes=("0", "0", "0", "100")))
            .evaluate(CODE, NOW, risk(), {}).reasons, ("VOLUME_BASELINE_UNAVAILABLE",))

    def test_momentum_weak_exit_and_no_duplicate_buy_for_position(self):
        candles = candle_event(closes=("110", "110", "110", "100"))
        result = ready_engine(candles=candles, book=quote(bid="99", ask="99.5")).evaluate(
            CODE, NOW, risk(), {CODE: {"quantity": 10, "average_price": "100"}})
        self.assertEqual((result.action, result.exit_reason), ("SELL", "MOMENTUM_WEAK"))
        held = ready_engine().evaluate(CODE, NOW, risk(), {CODE: 10})
        self.assertEqual(held.action, "HOLD")
        self.assertIn("COST_BASIS_UNAVAILABLE", held.reasons)

    def test_trade_is_optional_but_stale_trade_blocks_and_future_data_is_unused(self):
        engine = ready_engine()
        trade = observation("trade", {"currency": "KRW", "timestamp": NOW, "price": "111", "volume": "10"})
        engine.on_event(trade)
        result = engine.evaluate(CODE, NOW, risk(), {})
        self.assertEqual((result.action, result.indicators["price"]), ("BUY", Decimal(111)))
        future = instant(NOW) + timedelta(seconds=6)
        engine.on_event(quote(at=future, event_id="new-book"))
        self.assertIn("STALE_TRADE", engine.evaluate(CODE, future, risk(), {}).reasons)
        self.assertIn("FUTURE_OBSERVATION", engine.evaluate(CODE, NOW, risk(), {}).reasons)

    def test_completed_candles_only_merge_duplicates_and_conflicts(self):
        engine = StrategyEngine(strategy_config())
        initial = candle_event()
        first = observation("candles", {"candles": [bar(i) for i in (1, 2, 3)]}, event_id="initial")
        engine.on_event(first)
        # An incremental batch and one unfinished bar: only four completed bars.
        second = observation("candles", {"candles": [bar(4, "110", "400"), bar(5, "999", "9999")]}, event_id="increment")
        engine.on_event(second)
        engine.on_event(quote())
        before = engine.evaluate(CODE, NOW, risk(), {})
        self.assertEqual(before.action, "BUY")
        self.assertFalse(engine.on_event(second))
        self.assertEqual(before, engine.evaluate(CODE, NOW, risk(), {}))
        with self.assertRaises(ValueError):
            engine.on_event(replace(second, issues=("BAD",)))
        with self.assertRaises(ValueError):
            engine.on_event(observation("candles", {"candles": [bar(4, "109", "400")]}, event_id="correction"))
        self.assertTrue(engine.on_event(initial))  # identical overlapping bars

    def test_late_events_do_not_replace_latest_and_new_day_resets_history(self):
        engine = ready_engine()
        self.assertFalse(engine.on_event(quote(at="2026-11-13T10:04:00+09:00", bid="90", event_id="late")))
        self.assertEqual(engine.evaluate(CODE, NOW, risk(), {}).action, "BUY")
        tomorrow = "2026-11-16T10:04:01+09:00"
        engine.on_event(quote(at=tomorrow, event_id="next-day"))
        self.assertIn("CANDLES_UNAVAILABLE", engine.evaluate(CODE, tomorrow, risk(tomorrow), {}).reasons)
        self.assertFalse(engine.on_event(candle_event(event_id="old-day")))

    def test_session_identity_unknown_events_config_and_bad_position_rejected(self):
        engine = ready_engine()
        with self.assertRaises(ValueError):
            engine.on_event(replace(quote(), event_id="other-session", session_id="other"))
        with self.assertRaises(ValueError):
            engine.on_event(replace(quote(), event_id="daily", market="KRX_DAILY", kind="daily"))
        for overrides in ({"profile": "PAPER_READY"}, {"min_candles": 2}, {"volume_spike_ratio": "1"},
                          {"stop_loss_bps": "10000"}, {"quote_max_age_seconds": -1}):
            with self.assertRaises(ValueError):
                strategy_config(**overrides)
        with self.assertRaises(ValueError):
            engine.evaluate(CODE, NOW, risk(), {CODE: {"quantity": -1, "average_price": "100"}})
        for meta in ({"entry_blocked": "false"}, {"issues": "STALE_RANKINGS"}):
            with self.assertRaises(ValueError):
                engine.evaluate(CODE, NOW, risk(), {}, meta)

    def test_observe_only_contract_and_determinism_are_unchanged(self):
        self.assertEqual(MODE, "OBSERVE_ONLY")
        self.assertEqual(POLICY_VERSION, "daytrade-stage1-offline-2026-11-13.1")
        a, b = ready_engine(), ready_engine()
        self.assertEqual(a.evaluate(CODE, NOW, risk(), {}), b.evaluate(CODE, NOW, risk(), {}))
        changed = a.evaluate(CODE, NOW, risk(), {})
        changed.indicators["price"] = "999"
        self.assertEqual(a.evaluate(CODE, NOW, risk(), {}), b.evaluate(CODE, NOW, risk(), {}))


if __name__ == "__main__":
    unittest.main()
