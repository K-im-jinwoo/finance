"""Deterministic signal experiments, isolated from ledger execution and policy.

Only SYNTHETIC_PAPER_V1 is accepted. All numeric decision thresholds are explicit
fixture assumptions. No fees, orders, fills or production readiness are inferred.
BUY requires all four diagnostics (VWAP breakout, rising close, volume spike and
bid depth); score is their unweighted completion percentage, not a probability.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time
from decimal import Decimal
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
        if self.min_bid_notional is not None:
            object.__setattr__(self,'min_bid_notional',number(self.min_bid_notional,positive=True))
        if self.entry_start_hhmm is not None:
            if not isinstance(self.entry_start_hhmm,str) or re.fullmatch(r'\d{2}:\d{2}',self.entry_start_hhmm) is None:
                raise ValueError('HH:MM entry start required')
            hour,minute=map(int,self.entry_start_hhmm.split(':'))
            if not time(9) <= time(hour,minute) < time(14,50):
                raise ValueError('entry start must precede the market entry cutoff')
        if self.min_hold_seconds_for_weak_exit is not None and (type(self.min_hold_seconds_for_weak_exit) is not int or self.min_hold_seconds_for_weak_exit < 0):
            raise ValueError('nonnegative weak-exit grace required')


@dataclass(frozen=True)
class StrategySignal:
    action: str
    score: int
    reasons: tuple[str, ...]
    exit_reason: str | None
    reference_price: Decimal | None
    indicators: dict


@dataclass
class _SymbolState:
    day: str
    session_id: str
    latest: dict[str, MarketEvent] = field(default_factory=dict)
    bars: dict[str, dict] = field(default_factory=dict)


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

        def signal(action="HOLD", reasons=(), exit_reason=None, score=0):
            # Bad/stale quotes never become an executable reference, even on exit.
            reference = None if book["issues"] else book["ask" if action == "BUY" else "bid"]
            return StrategySignal(action, score, tuple(dict.fromkeys(reasons)), exit_reason, reference,
                                  dict(indicators))

        local = at.astimezone(KST)
        clock = local.time().replace(tzinfo=None)
        # Forced exit signals remain observable without a usable quote. The
        # caller must wait for a valid quote to fill; this module never fills.
        if quantity:
            if risk_state.phase in {Phase.TIME_EXITING, Phase.CLOSED} or clock >= time(15, 15):
                return signal("SELL", ("TIME_EXIT", *quote_issues), "TIME_EXIT", 100)
            if "LOSS_LOCKED" in risk_state.locks:
                return signal("SELL", ("LOSS_EXIT", *quote_issues), "LOSS_EXIT", 100)
            if not book["issues"] and basis is not None:
                pnl = (book["bid"] - basis) * 10000 / basis
                indicators["pnl_bps_gross"] = pnl
                if pnl <= -config.stop_loss_bps:
                    return signal("SELL", ("STOP_LOSS", *quote_issues), "STOP_LOSS", 100)
                if pnl >= config.take_profit_bps:
                    return signal("SELL", ("TAKE_PROFIT", *quote_issues), "TAKE_PROFIT", 100)

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
        price = number(trade.data["price"]) if trade is not None else number(bars[-1]["close"])
        vwap = approximate["value"]
        volume_ratio = number(bars[-1]["volume"]) / baseline
        indicators.update(bar_typical_vwap_approx=vwap, volume_spike_ratio=volume_ratio,
                          price=price, vwap_status="APPROXIMATION", candle_count=len(bars),
                          vwap_basis="RETAINED_COMPLETED_BARS_MAX_200",
                          price_vs_vwap_bps=(price - vwap) * 10000 / vwap,
                          volume_baseline_median=baseline)
        rising = number(bars[-1]["close"]) > number(bars[-2]["close"])
        if quantity:
            if price < vwap and number(bars[-1]["close"]) < number(bars[-2]["close"]):
                entry=positions.get(symbol,{})
                entered=entry.get('entry_at') if isinstance(entry,dict) else None
                if config.min_hold_seconds_for_weak_exit and (entered is None or
                        (at-instant(entered)).total_seconds() < config.min_hold_seconds_for_weak_exit):
                    return signal(reasons=('MOMENTUM_WEAK_GRACE',))
                return signal("SELL", ("MOMENTUM_WEAK",), "MOMENTUM_WEAK", 100)
            return signal(reasons=("POSITION_HELD",) if basis is not None else ("COST_BASIS_UNAVAILABLE",))
        checks = {"VWAP_BREAKOUT": price > vwap and number(bars[-2]["close"]) <= previous_approximate["value"],
                  "RISING_CLOSE": rising, "VOLUME_SPIKE": volume_ratio >= config.volume_spike_ratio,
                  "BID_DEPTH": book["bid_volume"] >= config.min_bid_volume and
                    (config.min_bid_notional is None or notional is not None and notional >= config.min_bid_notional)}
        score = 25 * sum(checks.values())
        if all(checks.values()):
            return signal("BUY", tuple(checks), score=score)
        return signal(reasons=tuple("MISSING_" + key for key, ok in checks.items() if not ok), score=score)
