"""Deterministic signal experiments, isolated from ledger execution and policy.

Only SYNTHETIC_PAPER_V1 is accepted. All numeric decision thresholds are explicit
fixture assumptions. No fees, orders, fills or production readiness are inferred.
BUY requires all four diagnostics (VWAP breakout, rising close, volume spike and
bid depth); score is their unweighted completion percentage, not a probability.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from decimal import Decimal, localcontext
from statistics import median
import re

from .contracts import KST, canonical, instant, number, symbol as checked_symbol
from .market import MarketEvent, bar_typical_vwap_approx
from .risk import Phase, RiskState
from .scanner import SCOPE, event_quality_issues, orderbook_metrics, risk_reasons


@dataclass(frozen=True, kw_only=True)
class StrategyConfig:
    min_candles: int
    volume_spike_ratio: str | Decimal
    min_bid_volume: str | Decimal
    max_spread_bps: str | Decimal
    quote_max_age_seconds: int
    candle_max_age_seconds: int
    take_profit_bps: str | Decimal
    stop_loss_bps: str | Decimal
    profile: str = "SYNTHETIC_PAPER_V1"
    min_bid_notional: str | Decimal | None = None
    entry_start_hhmm: str | None = None
    min_hold_seconds_for_weak_exit: int | None = None
    max_ask_bid_volume_ratio: str | Decimal | None = None
    breakout_lookback_bars: int | None = None
    weak_exit_min_vwap_gap_bps: str | Decimal | None = None
    trailing_stop_activation_bps: str | Decimal | None = None
    trailing_stop_retrace_bps: str | Decimal | None = None
    weak_exit_enabled: bool = True
    partial_take_profit_bps: str | Decimal | None = None
    partial_take_profit_ratio: str | Decimal | None = None
    post_partial_breakeven_bps: str | Decimal | None = None
    post_partial_trailing_retrace_bps: str | Decimal | None = None

    def __post_init__(self):
        if self.profile != "SYNTHETIC_PAPER_V1":
            raise ValueError("only the synthetic paper signal profile is supported")
        if type(self.min_candles) is not int or not 3 <= self.min_candles <= 200:
            raise ValueError("min_candles must be between 3 and 200")
        for key in ("volume_spike_ratio", "min_bid_volume", "max_spread_bps",
                    "take_profit_bps", "stop_loss_bps"):
            object.__setattr__(self, key, number(getattr(self, key), positive=True))
        if self.volume_spike_ratio <= 1 or self.stop_loss_bps >= 10000:
            raise ValueError("volume spike must exceed one; stop loss must be below 10000 bps")
        for value in (self.quote_max_age_seconds, self.candle_max_age_seconds):
            if type(value) is not int or value < 0:
                raise ValueError("explicit non-negative freshness seconds required")
        for key in ("min_bid_notional", "max_ask_bid_volume_ratio", "weak_exit_min_vwap_gap_bps"):
            if getattr(self, key) is not None:
                object.__setattr__(self, key, number(getattr(self, key), positive=True))
        if self.breakout_lookback_bars is not None and (
                type(self.breakout_lookback_bars) is not int or not 1 <= self.breakout_lookback_bars <= 200):
            raise ValueError("breakout_lookback_bars must be between 1 and 200")
        if (self.trailing_stop_activation_bps is None) != (self.trailing_stop_retrace_bps is None):
            raise ValueError("trailing stop activation and retrace bps must be configured together")
        if self.trailing_stop_activation_bps is not None:
            for key in ("trailing_stop_activation_bps", "trailing_stop_retrace_bps"):
                object.__setattr__(self, key, number(getattr(self, key), positive=True))
            if self.trailing_stop_retrace_bps >= self.trailing_stop_activation_bps:
                raise ValueError("trailing stop retrace must be smaller than activation bps")
        if self.entry_start_hhmm is not None:
            if not isinstance(self.entry_start_hhmm,str) or re.fullmatch(r'\d{2}:\d{2}',self.entry_start_hhmm) is None:
                raise ValueError('HH:MM entry start required')
            hour,minute=map(int,self.entry_start_hhmm.split(':'))
            if not time(9) <= time(hour,minute) < time(14,50):
                raise ValueError('entry start must precede the market entry cutoff')
        if self.min_hold_seconds_for_weak_exit is not None and (type(self.min_hold_seconds_for_weak_exit) is not int or self.min_hold_seconds_for_weak_exit < 0):
            raise ValueError('nonnegative weak-exit grace required')
        if type(self.weak_exit_enabled) is not bool:
            raise ValueError("weak_exit_enabled must be boolean")
        if (self.partial_take_profit_bps is None) != (self.partial_take_profit_ratio is None):
            raise ValueError("partial take profit bps and ratio must be configured together")
        if self.partial_take_profit_bps is not None:
            object.__setattr__(self, "partial_take_profit_bps", number(self.partial_take_profit_bps, positive=True))
            ratio = number(self.partial_take_profit_ratio, positive=True)
            if ratio >= 1:
                raise ValueError("partial_take_profit_ratio must be less than one")
            object.__setattr__(self, "partial_take_profit_ratio", ratio)
        for key in ("post_partial_breakeven_bps", "post_partial_trailing_retrace_bps"):
            value = getattr(self, key)
            if value is not None:
                if self.partial_take_profit_bps is None:
                    raise ValueError(f"{key} requires partial take profit")
                object.__setattr__(self, key, number(value, positive=key == "post_partial_trailing_retrace_bps"))


@dataclass(frozen=True)
class StrategySignal:
    action: str
    score: int
    reasons: tuple[str, ...]
    exit_reason: str | None
    reference_price: Decimal | None
    indicators: dict
    sell_quantity: int | None = None


@dataclass
class _SymbolState:
    day: str
    session_id: str
    latest: dict[str, MarketEvent] = field(default_factory=dict)
    bars: dict[str, dict] = field(default_factory=dict)
    bid_peaks: list[tuple[datetime, Decimal]] = field(default_factory=list)

    def peak_bid_since(self, entered, bid):
        if entered is None:
            return bid
        index = bisect_left(self.bid_peaks, instant(entered), key=lambda peak: peak[0])
        return max(bid, self.bid_peaks[index][1]) if index < len(self.bid_peaks) else bid


def _position(positions, code):
    """Accept long quantity or {quantity, average_price}; never guess cost basis."""
    item = positions.get(code, 0)
    quantity = item.get("quantity") if isinstance(item, dict) else item
    if type(quantity) is not int or quantity < 0:
        raise ValueError("non-negative integer position quantity required")
    basis = item.get("average_price") if isinstance(item, dict) else None
    return quantity, number(basis, positive=True) if basis is not None else None


class StrategyEngine:
    def __init__(self, config):
        if not isinstance(config, StrategyConfig):
            raise ValueError("StrategyConfig required")
        self.config = config
        self._states = {}
        self._seen = {}

    def on_event(self, event: MarketEvent):
        """Accumulate completed 1m 'candles', trade and orderbook MarketEvents.

        Repeated identities are no-ops; conflicting identities/candle versions
        raise instead of rewriting history. Late receive-order snapshots are
        ignored. A new KST day resets indicators; old days cannot replace it.
        """
        if (not isinstance(event, MarketEvent) or event.market != SCOPE
                or event.kind not in {"candles", "trade", "orderbook"}):
            raise ValueError("normalized integrated candles/trade/orderbook event required")
        checked_symbol(event.symbol)
        previous = self._seen.get(event.event_id)
        if previous is not None:
            if previous != event.fingerprint:
                raise ValueError("conflicting strategy event identity")
            return False
        received = instant(event.received_at_utc)
        day = received.astimezone(KST).date().isoformat()
        state = self._states.get(event.symbol)
        if state is not None and day < state.day:
            return False
        if state is None or day > state.day:
            state = _SymbolState(day, event.session_id)
        if state.session_id != event.session_id:
            raise ValueError("strategy session identity mismatch")
        prior = state.latest.get(event.kind)
        if prior is not None and received < instant(prior.received_at_utc):
            return False
        if event.kind == "candles":
            bars = dict(state.bars)
            for bar in event.data["candles"]:
                end = instant(bar["timestamp"])
                if end > received or end.astimezone(KST).date().isoformat() != day:
                    raise ValueError("same-day completed candles required")
                key = end.isoformat()
                if key in bars and bars[key] != bar:
                    raise ValueError("conflicting completed candle version")
                bars[key] = bar
            state.bars = {key: bars[key] for key in sorted(bars)[-200:]}
        elif event.kind == "orderbook":
            book = orderbook_metrics(event, now=received,
                max_age_seconds=self.config.quote_max_age_seconds, issuer=event.symbol)
            bid = book["bid"]
            if not book["issues"] and bid is not None:
                # A later bid of equal or greater value dominates every suffix containing the older bid.
                while state.bid_peaks and state.bid_peaks[-1][1] <= bid:
                    state.bid_peaks.pop()
                state.bid_peaks.append((received, bid))
        state.latest[event.kind] = event
        self._states[event.symbol] = state
        self._seen[event.event_id] = event.fingerprint
        return True

    def _candles(self, state):
        latest = state.latest.get("candles")
        if latest is None:
            return None
        data = {**latest.data, "candles": list(state.bars.values())}
        return replace(latest, payload_json=canonical(data))

    def evaluate(self, symbol, now, risk_state, positions, candidate_meta=None):
        checked_symbol(symbol)
        at = instant(now)
        if not isinstance(risk_state, RiskState) or not isinstance(positions, dict):
            raise ValueError("RiskState and positions mapping required")
        quantity, basis = _position(positions, symbol)
        state = self._states.get(symbol)
        config = self.config
        book = orderbook_metrics(state.latest.get("orderbook") if state else None,
            now=at, max_age_seconds=config.quote_max_age_seconds, issuer=symbol)
        indicators = {"profile": config.profile, "scope": SCOPE, "order_strength": "UNAVAILABLE",
            "tick_vwap": "UNAVAILABLE", "bar_typical_vwap_approx": None,
            "volume_spike_ratio": None, "spread_bps": book["spread_bps"],
            "best_bid_volume": book["bid_volume"], "pnl_bps_gross": None}
        notional=book['bid']*book['bid_volume'] if book['bid'] is not None and book['bid_volume'] is not None else None
        raw_book=state.latest.get('orderbook') if state else None
        bid_total=sum((number(v['volume']) for v in raw_book.data['bids']),Decimal(0)) if raw_book else 0
        ask_total=sum((number(v['volume']) for v in raw_book.data['asks']),Decimal(0)) if raw_book else 0
        indicators.update(best_bid_notional=notional,ask_bid_volume_ratio=ask_total/bid_total
            if bid_total and raw_book.data['asks'] else None)
        quote_issues = list(book["issues"])
        if book["spread_bps"] is not None and book["spread_bps"] > config.max_spread_bps:
            quote_issues.append("WIDE_SPREAD")

        def signal(action="HOLD", reasons=(), exit_reason=None, score=0, sell_quantity=None):
            # Bad/stale quotes never become an executable reference, even on exit.
            reference = None if book["issues"] else book["ask" if action == "BUY" else "bid"]
            return StrategySignal(action, score, tuple(dict.fromkeys(reasons)), exit_reason, reference,
                                  dict(indicators), sell_quantity)

        def exit_signal(reason, sell_quantity=None):
            return signal("SELL", (reason, *quote_issues), reason, 100, sell_quantity)

        local = at.astimezone(KST)
        clock = local.time().replace(tzinfo=None)
        # Forced exit signals remain observable without a usable quote. The
        # caller must wait for a valid quote to fill; this module never fills.
        if quantity:
            if risk_state.phase in {Phase.TIME_EXITING, Phase.CLOSED} or clock >= time(15, 15):
                return exit_signal("TIME_EXIT")
            if "LOSS_LOCKED" in risk_state.locks:
                return exit_signal("LOSS_EXIT")
            if not book["issues"] and basis is not None:
                pnl = (book["bid"] - basis) * 10000 / basis
                indicators["pnl_bps_gross"] = pnl
                if pnl <= -config.stop_loss_bps:
                    return exit_signal("STOP_LOSS")
                entry = positions[symbol] if isinstance(positions[symbol], dict) else {}
                entered = entry.get("entry_at")
                if config.partial_take_profit_bps is not None:
                    if entry.get("partial_tp_done") is True:
                        if config.post_partial_breakeven_bps is not None and pnl <= config.post_partial_breakeven_bps:
                            return exit_signal("BREAKEVEN_STOP")
                        if config.post_partial_trailing_retrace_bps is not None:
                            peak_bid = state.peak_bid_since(entered, book["bid"])
                            indicators["peak_bid_pnl_bps_gross"] = (peak_bid - basis) * 10000 / basis
                            retrace = (peak_bid - book["bid"]) * 10000
                            if retrace >= basis * config.post_partial_trailing_retrace_bps:
                                return exit_signal("TRAILING_STOP")
                    elif pnl >= config.partial_take_profit_bps:
                        ratio = config.partial_take_profit_ratio
                        # Keep the product exact before dropping the fractional share.
                        with localcontext() as context:
                            context.prec = Decimal(quantity).adjusted() + 1 + len(ratio.as_tuple().digits)
                            shares = int(quantity * ratio)
                        if 0 < shares < quantity:
                            return exit_signal("PARTIAL_TAKE_PROFIT", shares)
                        return exit_signal("TAKE_PROFIT")
                if pnl >= config.take_profit_bps:
                    return exit_signal("TAKE_PROFIT")
                if config.trailing_stop_activation_bps is not None and state is not None:
                    post_highs = [number(b["high"]) for b in state.bars.values()
                                  if entered is not None and instant(b["timestamp"]) > instant(entered)]
                    peak_price = max([book["bid"], *post_highs])
                    peak_pnl = (peak_price - basis) * 10000 / basis
                    indicators["peak_pnl_bps_gross"] = peak_pnl
                    if (peak_pnl >= config.trailing_stop_activation_bps
                            and pnl <= peak_pnl - config.trailing_stop_retrace_bps):
                        return signal("SELL", ("TAKE_PROFIT", "TRAILING_STOP", *quote_issues), "TAKE_PROFIT", 100)

        reasons = list(quote_issues)
        if state is None:
            return signal(reasons=(*reasons, "MARKET_DATA_UNAVAILABLE"))
        if state.day != local.date().isoformat():
            reasons.append("SESSION_DATE_MISMATCH")
        if not quantity:
            if config.entry_start_hhmm is not None and clock < time.fromisoformat(config.entry_start_hhmm):
                reasons.append('ENTRY_WINDOW_WARMUP')
            if (not risk_state.candidate_entry_allowed or not time(9) <= clock < time(14, 50)
                    or risk_state.session_date != local.date().isoformat()):
                reasons.append("ENTRY_NOT_ALLOWED")
            if candidate_meta is not None:
                reasons.extend(risk_reasons(candidate_meta))
                if ("entry_blocked" in candidate_meta and type(candidate_meta["entry_blocked"]) is not bool
                        or not isinstance(candidate_meta.get("issues", ()), (tuple, list))
                        or any(not isinstance(issue, str) for issue in candidate_meta.get("issues", ()))):
                    raise ValueError("candidate quality flags must be boolean and a list of issue names")
                if candidate_meta.get("symbol", symbol) != symbol:
                    reasons.append("CANDIDATE_SYMBOL_MISMATCH")
                if candidate_meta.get("scope", SCOPE) != SCOPE:
                    reasons.append("SCOPE_MISMATCH")
                if candidate_meta.get("entry_blocked"):
                    reasons.append("CANDIDATE_ENTRY_BLOCKED")
                if candidate_meta.get('warmup_until') and at < instant(candidate_meta['warmup_until']):
                    reasons.append('SUBSCRIPTION_WARMUP')
                reasons.extend(candidate_meta.get("issues", ()))
                if "valid_until" in candidate_meta and (candidate_meta["valid_until"] is None
                        or at > instant(candidate_meta["valid_until"])):
                    reasons.append("STALE_RANKINGS")
        candles = self._candles(state)
        if candles is None:
            return signal(reasons=(*reasons, "CANDLES_UNAVAILABLE"))
        reasons.extend(event_quality_issues(candles, at, config.candle_max_age_seconds))
        bars = candles.data["candles"]
        if len(bars) < config.min_candles:
            return signal(reasons=(*reasons, "INSUFFICIENT_CANDLES"))
        trade = state.latest.get("trade")
        if trade is not None:
            reasons.extend(event_quality_issues(trade, at, config.quote_max_age_seconds))
        if reasons:
            return signal(reasons=reasons)

        approximate = bar_typical_vwap_approx(candles)
        previous_event = replace(candles, payload_json=canonical({**candles.data, "candles": bars[:-1]}))
        previous_approximate = bar_typical_vwap_approx(previous_event)
        baseline = median(number(bar["volume"]) for bar in bars[:-1])
        if approximate["value"] is None or previous_approximate["value"] is None or baseline == 0:
            return signal(reasons=("VOLUME_BASELINE_UNAVAILABLE",))
        last_close = number(bars[-1]["close"])
        prior_close = number(bars[-2]["close"])
        price = number(trade.data["price"]) if trade is not None else last_close
        vwap = approximate["value"]
        volume_ratio = number(bars[-1]["volume"]) / baseline
        indicators.update(bar_typical_vwap_approx=vwap, volume_spike_ratio=volume_ratio,
                          price=price, vwap_status="APPROXIMATION", candle_count=len(bars),
                          vwap_basis="RETAINED_COMPLETED_BARS_MAX_200",
                          price_vs_vwap_bps=(price - vwap) * 10000 / vwap,
                          volume_baseline_median=baseline)
        rising = last_close > prior_close
        if quantity:
            vwap_gap_bps = (vwap - price) * 10000 / vwap
            weak_vwap = (vwap_gap_bps >= config.weak_exit_min_vwap_gap_bps and last_close < vwap
                         if config.weak_exit_min_vwap_gap_bps is not None else price < vwap)
            if config.weak_exit_enabled and weak_vwap and last_close < prior_close:
                entry=positions.get(symbol,{})
                entered=entry.get('entry_at') if isinstance(entry,dict) else None
                if config.min_hold_seconds_for_weak_exit and (entered is None or
                        (at-instant(entered)).total_seconds() < config.min_hold_seconds_for_weak_exit):
                    return signal(reasons=('MOMENTUM_WEAK_GRACE',))
                return signal("SELL", ("MOMENTUM_WEAK",), "MOMENTUM_WEAK", 100)
            return signal(reasons=("POSITION_HELD",) if basis is not None else ("COST_BASIS_UNAVAILABLE",))
        vwap_breakout = price > vwap and prior_close <= previous_approximate["value"]
        if vwap_breakout and config.breakout_lookback_bars is not None:
            recent_window = bars[-min(len(bars), config.breakout_lookback_bars + 1):-1]
            recent_high = max(number(b["close"]) for b in recent_window)
            last_open = number(bars[-1].get("open", bars[-1]["close"]))
            first_open = number(bars[0].get("open", bars[0]["close"]))
            vwap_breakout = (last_close > vwap and vwap > previous_approximate["value"]
                and last_close > last_open and last_close >= first_open
                and last_close > recent_high
                and (last_close - price) * 10000 / last_close <= Decimal("50"))
        bid_depth = (book["bid_volume"] >= config.min_bid_volume
            and (config.min_bid_notional is None or notional is not None and notional >= config.min_bid_notional)
            and (config.max_ask_bid_volume_ratio is None or indicators["ask_bid_volume_ratio"] is not None
                 and indicators["ask_bid_volume_ratio"] <= config.max_ask_bid_volume_ratio))
        checks = {"VWAP_BREAKOUT": vwap_breakout,
                  "RISING_CLOSE": rising, "VOLUME_SPIKE": volume_ratio >= config.volume_spike_ratio,
                  "BID_DEPTH": bid_depth}
        score = 25 * sum(checks.values())
        if all(checks.values()):
            return signal("BUY", tuple(checks), score=score)
        return signal(reasons=tuple("MISSING_" + key for key, ok in checks.items() if not ok), score=score)
