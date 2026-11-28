"""FC1 regressions using synthetic quotes and temporary paper ledgers."""
from datetime import timedelta
from decimal import Decimal
import tempfile
import unittest

from stock_assistant.daytrade.bot import _observation
from stock_assistant.daytrade.contracts import instant
from stock_assistant.daytrade.daemon import _resume, open_or_resume_session
from stock_assistant.daytrade.live_config import live_settings, merge_live_settings
from stock_assistant.daytrade.strategy import StrategyEngine
from stock_assistant.daytrade.telegram import format_fill_telegram_message
from test_daytrade_scanner_strategy import (
    CODE, NOW, candle_event, quote, ready_engine, risk, strategy_config,
)


FC1 = {
    "stop_loss_bps": "350", "take_profit_bps": "10000", "weak_exit_enabled": False,
    "partial_take_profit_bps": "500", "partial_take_profit_ratio": "0.5",
    "post_partial_breakeven_bps": "0", "post_partial_trailing_retrace_bps": "200",
    "trailing_stop_activation_bps": None, "trailing_stop_retrace_bps": None,
}


def seconds_after(offset):
    return instant(NOW) + timedelta(seconds=offset)


def position(quantity=10, *, partial_done=False, entered=NOW):
    return {CODE: {"quantity": quantity, "average_price": "10000",
                   "entry_at": entered, "partial_tp_done": partial_done}}


def orderbook(runner, offset, bid, *, volume=1000, step=False):
    at = seconds_after(offset)
    data = {"feed": "orderbook", "symbol": CODE, "received_monotonic": offset,
            "payload": {"currency": "KRW", "timestamp": at.isoformat(),
                        "bids": [{"price": str(bid), "volume": str(volume)}],
                        "asks": [{"price": str(bid + 1), "volume": "1000"}]}}
    if step:
        runner.step({"id": f"book-{offset}", "at": at, "kind": "MARKET", "data": data})
        return
    event_id, _ = runner.emit("MARKET", data, at)
    event = _observation({"at": at, "data": data}, runner.config.quality, event_id=event_id)
    runner.strategy.on_event(event)
    return event


def buy(runner, offset=0):
    intent, _ = runner.emit("INTENT", {"symbol": CODE, "side": "BUY", "quantity": 10},
                            seconds_after(offset))
    book = orderbook(runner, offset + 2, 9999)
    runner.emit("FILL", {"intent_id": intent, "quote_event_id": book.event_id},
                seconds_after(offset + 2))


class FC1StrategyTests(unittest.TestCase):
    def test_configuration_validates_pairs_and_boundaries(self):
        self.assertTrue(strategy_config().weak_exit_enabled)
        pair = {"partial_take_profit_bps": "500", "partial_take_profit_ratio": "0.5"}
        for settings in (
            {"weak_exit_enabled": 0}, {"partial_take_profit_bps": "500"},
            {"partial_take_profit_ratio": "0.5"}, {"post_partial_breakeven_bps": "0"},
            {"post_partial_trailing_retrace_bps": "200"}, pair | {"partial_take_profit_bps": "0"},
            pair | {"partial_take_profit_ratio": "0"}, pair | {"partial_take_profit_ratio": "1"},
            pair | {"post_partial_breakeven_bps": "-1"}, pair | {"post_partial_trailing_retrace_bps": "0"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                strategy_config(**settings)
        config = strategy_config(**FC1)
        self.assertEqual(config.partial_take_profit_ratio, Decimal("0.5"))
        self.assertEqual(config.post_partial_breakeven_bps, Decimal("0"))

    def test_five_percent_partial_rounds_down_and_one_share_exits_in_full(self):
        book = quote(bid="10500", ask="10501")
        engine = ready_engine(config=strategy_config(**FC1), book=book)
        partial = engine.evaluate(CODE, NOW, risk(), position(11))
        self.assertEqual((partial.action, partial.exit_reason, partial.reasons, partial.sell_quantity),
                         ("SELL", "PARTIAL_TAKE_PROFIT", ("PARTIAL_TAKE_PROFIT",), 5))
        full = engine.evaluate(CODE, NOW, risk(), position(1))
        self.assertEqual((full.exit_reason, full.sell_quantity), ("TAKE_PROFIT", None))

    def test_partial_quantity_floors_a_ratio_just_below_a_whole_share_boundary(self):
        config = strategy_config(**(FC1 | {"partial_take_profit_ratio": "0.59999999999999999999999999999"}))
        engine = ready_engine(config=config, book=quote(bid="10500", ask="10501"))
        self.assertEqual(engine.evaluate(CODE, NOW, risk(), position(5)).sell_quantity, 2)

    def test_post_partial_peak_excludes_pre_entry_and_invalid_quotes(self):
        engine = StrategyEngine(strategy_config(**FC1))
        for offset, bid, ask in ((0, 12000, 12001), (1, 11000, 11001),
                                 (2, 13000, 12999), (3, 10801, 10802), (4, 10800, 10801)):
            at = seconds_after(offset)
            engine.on_event(quote(at=at.isoformat(), bid=str(bid), ask=str(ask), event_id=f"book-{offset}"))
            if offset in (3, 4):
                result = engine.evaluate(CODE, at, risk(at), position(partial_done=True, entered=seconds_after(1)))
                self.assertEqual(result.indicators["peak_bid_pnl_bps_gross"], Decimal("1000"))
                self.assertEqual(result.exit_reason, None if offset == 3 else "TRAILING_STOP")
                self.assertIsNone(result.sell_quantity)

    def test_trailing_retrace_is_inclusive_with_repeating_pnl_decimals(self):
        engine = StrategyEngine(strategy_config(**FC1))
        engine.on_event(quote(bid="16550", ask="16551", event_id="peak"))
        at = seconds_after(1)
        engine.on_event(quote(at=at.isoformat(), bid="16250", ask="16251", event_id="retrace"))
        held = position(partial_done=True)
        held[CODE]["average_price"] = "15000"
        self.assertEqual(engine.evaluate(CODE, at, risk(at), held).exit_reason, "TRAILING_STOP")

    def test_breakeven_is_inclusive_and_hard_stop_has_priority(self):
        for bid, expected in ((10000, "BREAKEVEN_STOP"), (9650, "STOP_LOSS")):
            with self.subTest(bid=bid):
                engine = ready_engine(config=strategy_config(**FC1), book=quote(bid=str(bid), ask=str(bid + 1)))
                signal = engine.evaluate(CODE, NOW, risk(), position(partial_done=True))
                self.assertEqual((signal.exit_reason, signal.sell_quantity), (expected, None))

    def test_weak_exit_can_be_disabled(self):
        inputs = {"candles": candle_event(closes=("110", "110", "110", "109")),
                  "book": quote(bid="109", ask="110")}
        held = {CODE: {"quantity": 2, "average_price": "110"}}
        for enabled, expected in ((True, "MOMENTUM_WEAK"), (False, None)):
            with self.subTest(enabled=enabled):
                engine = ready_engine(config=strategy_config(weak_exit_enabled=enabled), **inputs)
                self.assertEqual(engine.evaluate(CODE, NOW, risk(), held).exit_reason, expected)


class FC1PaperTests(unittest.TestCase):
    def open_session(self, root):
        session = open_or_resume_session(root, "2026-11-13", live_settings={"strategy_config": FC1})
        self.addCleanup(session.close)
        session.on_session(normal_session_verified=True, at=NOW)
        return session

    def test_unfilled_partial_intent_yields_to_stop_and_time_exit(self):
        for exit_reason in ("STOP_LOSS", "TIME_EXIT"):
            with self.subTest(exit_reason=exit_reason), tempfile.TemporaryDirectory() as root:
                session = self.open_session(root)
                runner = session._runner
                buy(runner)
                orderbook(runner, 3, 10600)
                runner.evaluate(seconds_after(3))
                partial, = runner.pending(code=CODE, side="SELL")
                self.assertEqual(runner.pending(side="SELL")[partial]["quantity"], 5)
                self.assertEqual(runner.entry_history(seconds_after(3))["partial_tp_done"], [])
                if exit_reason == "STOP_LOSS":
                    orderbook(runner, 4, 9600, step=True)
                else:
                    closeout = instant("2026-11-13T15:15:00+09:00")
                    offset = int((closeout - instant(NOW)).total_seconds())
                    orderbook(runner, offset, 10600, step=True)
                replacement, = runner.pending(code=CODE, side="SELL")
                self.assertEqual(runner.ledger.state()["intents"][partial]["filled"], 0)
                self.assertIn(partial, runner.inactive)
                self.assertTrue(any(row.get("intent_id") == partial for row in runner.meta["decisions"]))
                self.assertEqual(runner.pending(side="SELL")[replacement]["quantity"], 10)
                self.assertEqual(runner.meta["decisions"][-1]["reason"], exit_reason)
                # Reopen before the runner can checkpoint the cancellation.
                session_root = session.ledger.path.parent.parent
                session.close()
                with _resume(session_root, None) as resumed:
                    self.assertEqual(list(resumed._runner.pending(code=CODE, side="SELL")), [replacement])

    def test_committed_partial_fills_complete_only_at_target_and_reset_after_flat(self):
        with tempfile.TemporaryDirectory() as root:
            runner = self.open_session(root)._runner
            buy(runner)
            orderbook(runner, 3, 10600)
            runner.evaluate(seconds_after(3))
            partial, = runner.pending(side="SELL")
            # At 50% participation this book fills two of the five shares.
            book = orderbook(runner, 6, 10600, volume=4)
            runner.fill_pending(book, seconds_after(6))
            remaining = runner.pending(side="SELL")[partial]
            self.assertTrue(0 < remaining["filled"] < remaining["quantity"])
            self.assertEqual(runner.entry_history(seconds_after(6))["partial_tp_done"], [])
            book = orderbook(runner, 7, 10600)
            runner.fill_pending(book, seconds_after(7))
            self.assertEqual(runner.entry_history(seconds_after(7))["partial_tp_done"], [CODE])
            orderbook(runner, 8, 10000)
            runner.evaluate(seconds_after(8))
            self.assertEqual(runner.meta["decisions"][-1]["reason"], "BREAKEVEN_STOP")
            book = orderbook(runner, 10, 10000)
            runner.fill_pending(book, seconds_after(10))
            self.assertFalse(runner.ledger.state()["positions"])
            buy(runner, offset=11)
            self.assertEqual(runner.entry_history(seconds_after(13))["partial_tp_done"], [])
            orderbook(runner, 14, 10600)
            runner.evaluate(seconds_after(14))
            self.assertEqual(runner.meta["decisions"][-1]["reason"], "PARTIAL_TAKE_PROFIT")

    def test_resume_restores_bid_peak_for_open_position(self):
        with tempfile.TemporaryDirectory() as root:
            session = self.open_session(root)
            runner = session._runner
            buy(runner)
            orderbook(runner, 3, 11000)
            runner.evaluate(seconds_after(3))
            book = orderbook(runner, 5, 10900)
            runner.fill_pending(book, seconds_after(5))
            orderbook(runner, 6, 10790)
            session_root = session.ledger.path.parent.parent
            session.close()
            with _resume(session_root, None) as resumed:
                self.assertFalse(resumed.orders_enabled)
                resumed._runner.evaluate(seconds_after(6))
                latest = resumed._runner.meta["signals"][-1]
                self.assertEqual(latest["exit_reason"], "TRAILING_STOP")
                self.assertGreater(latest["indicators"]["peak_bid_pnl_bps_gross"], Decimal("950"))


class FC1PresentationTests(unittest.TestCase):
    def test_telegram_formats_exit_labels(self):
        for reason, label in (("PARTIAL_TAKE_PROFIT", "1차 분할 익절"), ("BREAKEVEN_STOP", "본절 보호 청산")):
            with self.subTest(reason=reason):
                fill = {"symbol": CODE, "side": "SELL", "quantity": 1, "gross": "10500",
                        "at": NOW, "fee": "1", "tax": "1", "signal": {"exit_reason": reason}}
                message = format_fill_telegram_message(fill, symbol_names={},
                    account={"cash": "10000", "realized_pnl_net": "500"}, positions={CODE: 1})
                self.assertIn(label, message)

    def test_live_settings_replace_legacy_trailing_and_keep_entry_controls(self):
        settings = merge_live_settings({"strategy_config": {
            "trailing_stop_activation_bps": "90", "trailing_stop_retrace_bps": "40",
        }}, live_settings())
        self.assertEqual({key: settings["strategy_config"].get(key) for key in FC1}, FC1)
        self.assertEqual(settings["max_daily_entries"], 5)
        self.assertEqual(settings["reentry_cooldown_seconds"], 1200)
