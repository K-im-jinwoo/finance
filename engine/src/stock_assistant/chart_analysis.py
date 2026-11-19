"""Deterministic multi-indicator technical chart analysis over daily OHLCV bars.

Pure standard-library calculations (no external TA dependencies, no network or DB
writes). Produces both full series for multi-panel chart rendering and compact
JSON summaries for Investment Committee review.
"""
from __future__ import annotations

from decimal import Decimal
import math
from typing import Any

from .models import OHLCV


def _r(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def _sma_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    running = 0.0
    for idx, val in enumerate(values):
        running += val
        if idx >= period:
            running -= values[idx - period]
        if idx + 1 >= period:
            out.append(running / period)
        else:
            out.append(None)
    return out


def _ema_series(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    ema = sum(values[:period]) / period
    out.append(ema)
    alpha = 2.0 / (period + 1)
    for val in values[period:]:
        ema = (val - ema) * alpha + ema
        out.append(ema)
    return out


def _moving_averages(closes: list[float]) -> dict[str, Any]:
    series = {p: _sma_series(closes, p) for p in (5, 20, 60, 120)}
    latest = {p: series[p][-1] for p in (5, 20, 60, 120)}
    close = closes[-1]
    s5, s20, s60, s120 = latest[5], latest[20], latest[60], latest[120]

    if s5 is not None and s20 is not None and s60 is not None:
        if s120 is not None:
            if s5 > s20 > s60 > s120:
                alignment = "BULLISH_FULL"
            elif s5 < s20 < s60 < s120:
                alignment = "BEARISH_FULL"
            elif s5 > s20 > s60:
                alignment = "BULLISH_MID"
            else:
                alignment = "MIXED"
        elif s5 > s20 > s60:
            alignment = "BULLISH_MID"
        elif s5 < s20 < s60:
            alignment = "BEARISH_MID"
        else:
            alignment = "MIXED"
    else:
        alignment = "MIXED"

    recent_cross = "NONE"
    lookback = min(5, len(closes) - 1)
    for fast_p, slow_p, label in ((20, 60, "20_60"), (5, 20, "5_20")):
        fast, slow = series[fast_p], series[slow_p]
        for offset in range(1, lookback + 1):
            i = len(closes) - offset
            if (
                i >= 1
                and fast[i] is not None and slow[i] is not None
                and fast[i - 1] is not None and slow[i - 1] is not None
            ):
                if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
                    recent_cross = f"GOLDEN_{label}"
                    break
                if fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
                    recent_cross = f"DEAD_{label}"
                    break
        if recent_cross != "NONE":
            break

    disparity20 = ((close / s20) - 1.0) * 100.0 if s20 and s20 > 0 else None
    return {
        "series": series,
        "sma5": _r(s5, 1),
        "sma20": _r(s20, 1),
        "sma60": _r(s60, 1),
        "sma120": _r(s120, 1),
        "alignment": alignment,
        "recent_cross": recent_cross,
        "disparity20_pct": _r(disparity20, 2),
    }


def _bollinger(closes: list[float], period: int = 20, num_std: float = 2.0) -> dict[str, Any]:
    upper: list[float | None] = []
    middle: list[float | None] = []
    lower: list[float | None] = []
    bw_series: list[float | None] = []
    for i in range(len(closes)):
        if i + 1 < period:
            upper.append(None)
            middle.append(None)
            lower.append(None)
            bw_series.append(None)
            continue
        window = closes[i + 1 - period:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(max(0.0, variance))
        u, l = mean + num_std * std, mean - num_std * std
        upper.append(u)
        middle.append(mean)
        lower.append(l)
        bw_series.append(((u - l) / mean * 100.0) if mean > 0 else 0.0)

    u_last, m_last, l_last = upper[-1], middle[-1], lower[-1]
    close = closes[-1]
    if u_last is not None and l_last is not None and (u_last - l_last) > 1e-9:
        percent_b = (close - l_last) / (u_last - l_last)
    else:
        percent_b = None
    bw_last = bw_series[-1]
    recent_bws = sorted(x for x in bw_series[-60:] if x is not None)
    squeeze = bool(
        bw_last is not None and len(recent_bws) >= 10
        and bw_last <= recent_bws[max(0, int(len(recent_bws) * 0.2) - 1)]
    )
    if u_last is None or l_last is None or m_last is None:
        state = "UNAVAILABLE"
    elif close > u_last:
        state = "ABOVE_UPPER"
    elif close < l_last:
        state = "BELOW_LOWER"
    elif close >= m_last:
        state = "UPPER_HALF"
    else:
        state = "LOWER_HALF"

    return {
        "series": {"upper": upper, "middle": middle, "lower": lower},
        "upper": _r(u_last, 1),
        "middle": _r(m_last, 1),
        "lower": _r(l_last, 1),
        "percent_b": _r(percent_b, 2),
        "bandwidth_pct": _r(bw_last, 2),
        "squeeze": squeeze,
        "state": state,
    }


def _rsi(closes: list[float], period: int = 14) -> dict[str, Any]:
    n = len(closes)
    series: list[float | None] = [None] * n
    if n <= period:
        return {"series": series, "value": None, "zone": "UNAVAILABLE", "divergence": "NONE"}
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, n)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def to_rsi(g: float, l: float) -> float:
        if g == 0.0 and l == 0.0:
            return 50.0
        if l == 0.0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    series[period] = to_rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        series[i] = to_rsi(avg_gain, avg_loss)

    val = series[-1]
    if val is None:
        zone = "UNAVAILABLE"
    elif val >= 70.0:
        zone = "OVERBOUGHT"
    elif val >= 55.0:
        zone = "BULLISH"
    elif val > 45.0:
        zone = "NEUTRAL"
    elif val > 30.0:
        zone = "WEAK"
    else:
        zone = "OVERSOLD"

    # Detect simple 30-bar swing divergence (last 10 bars vs prior 20 bars).
    divergence = "NONE"
    if n >= 35 and all(series[i] is not None for i in range(n - 30, n)):
        prior_slice = range(n - 30, n - 10)
        recent_slice = range(n - 10, n)
        p_low_i = min(prior_slice, key=lambda idx: closes[idx])
        r_low_i = min(recent_slice, key=lambda idx: closes[idx])
        p_high_i = max(prior_slice, key=lambda idx: closes[idx])
        r_high_i = max(recent_slice, key=lambda idx: closes[idx])
        if (
            closes[r_low_i] < closes[p_low_i] * 0.99
            and (series[r_low_i] or 0.0) > (series[p_low_i] or 0.0) + 3.0
        ):
            divergence = "BULLISH_DIVERGENCE"
        elif (
            closes[r_high_i] > closes[p_high_i] * 1.01
            and (series[r_high_i] or 0.0) < (series[p_high_i] or 0.0) - 3.0
        ):
            divergence = "BEARISH_DIVERGENCE"

    return {
        "series": series,
        "value": _r(val, 1),
        "zone": zone,
        "divergence": divergence,
    }


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Any]:
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid_indices = [i for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[float | None] = [None] * len(closes)
    histogram: list[float | None] = [None] * len(closes)
    if len(valid_indices) >= signal:
        valid_values = [macd_line[i] for i in valid_indices]
        sig_values = _ema_series([v for v in valid_values if v is not None], signal)
        for idx, sv in zip(valid_indices, sig_values):
            signal_line[idx] = sv
            if sv is not None and macd_line[idx] is not None:
                histogram[idx] = macd_line[idx] - sv

    m_last, s_last, h_last = macd_line[-1], signal_line[-1], histogram[-1]
    h_prev = histogram[-2] if len(histogram) >= 2 else None
    if m_last is None or s_last is None or h_last is None:
        state = "UNAVAILABLE"
    elif h_prev is not None and h_prev <= 0 < h_last:
        state = "BULLISH_CROSS"
    elif h_prev is not None and h_prev >= 0 > h_last:
        state = "BEARISH_CROSS"
    elif h_last > 0:
        state = "ABOVE_SIGNAL_EXPANDING" if (h_prev is None or h_last >= h_prev) else "ABOVE_SIGNAL_CONTRACTING"
    else:
        state = "BELOW_SIGNAL_IMPROVING" if (h_prev is not None and h_last > h_prev) else "BELOW_SIGNAL_WEAKENING"

    return {
        "series": {"macd": macd_line, "signal": signal_line, "histogram": histogram},
        "macd": _r(m_last, 1),
        "signal": _r(s_last, 1),
        "histogram": _r(h_last, 1),
        "state": state,
    }


def _ichimoku(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    n = len(closes)
    if n < 52:
        return {"tenkan": None, "kijun": None, "span_a": None, "span_b": None, "cloud_position": "UNAVAILABLE"}

    def mid(end_idx: int, period: int) -> float:
        start = end_idx + 1 - period
        return (max(highs[start:end_idx + 1]) + min(lows[start:end_idx + 1])) / 2.0

    tenkan = mid(n - 1, 9)
    kijun = mid(n - 1, 26)
    # Current bar's Ichimoku cloud is shifted forward 26 bars, so read values computed at n - 1 - 26.
    shift_idx = n - 1 - 26
    if shift_idx >= 51:
        span_a = (mid(shift_idx, 9) + mid(shift_idx, 26)) / 2.0
        span_b = mid(shift_idx, 52)
    else:
        # Fallback to current leading edge when history is 61..77 bars.
        span_a = (tenkan + kijun) / 2.0
        span_b = mid(n - 1, 52)

    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)
    close = closes[-1]
    if close > cloud_top:
        pos = "ABOVE_CLOUD"
    elif close < cloud_bottom:
        pos = "BELOW_CLOUD"
    else:
        pos = "IN_CLOUD"
    return {
        "tenkan": _r(tenkan, 1),
        "kijun": _r(kijun, 1),
        "span_a": _r(span_a, 1),
        "span_b": _r(span_b, 1),
        "cloud_position": pos,
    }


def _cluster_levels(levels: list[float], tolerance_pct: float = 0.02) -> list[float]:
    if not levels:
        return []
    ordered = sorted(levels)
    clusters: list[list[float]] = [[ordered[0]]]
    for level in ordered[1:]:
        anchor = sum(clusters[-1]) / len(clusters[-1])
        if anchor > 0 and abs(level - anchor) / anchor <= tolerance_pct:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    return [sum(group) / len(group) for group in clusters]


def _support_resistance(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    n = len(closes)
    close = closes[-1]
    swing_highs: list[float] = []
    swing_lows: list[float] = []
    window = 3
    for i in range(window, n - window):
        if highs[i] == max(highs[i - window:i + window + 1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i - window:i + window + 1]):
            swing_lows.append(lows[i])

    # Always include period extremes so range-bound or flat charts still have valid bounds.
    swing_highs.extend([max(highs[-20:]), max(highs[-60:])])
    swing_lows.extend([min(lows[-20:]), min(lows[-60:])])

    clustered_supports = _cluster_levels([x for x in swing_lows if x < close * 0.999])
    clustered_resistances = _cluster_levels([x for x in swing_highs if x > close * 1.001])

    supports = sorted(clustered_supports, reverse=True)[:2]
    resistances = sorted(clustered_resistances)[:2]

    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None
    support_pct = ((nearest_support / close) - 1.0) * 100.0 if nearest_support and close > 0 else None
    resistance_pct = ((nearest_resistance / close) - 1.0) * 100.0 if nearest_resistance and close > 0 else None

    return {
        "supports": [_r(x, 0) for x in supports],
        "resistances": [_r(x, 0) for x in resistances],
        "nearest_support": _r(nearest_support, 0),
        "nearest_resistance": _r(nearest_resistance, 0),
        "nearest_support_pct": _r(support_pct, 2),
        "nearest_resistance_pct": _r(resistance_pct, 2),
        "high_60d": _r(max(highs[-60:]), 0),
        "low_60d": _r(min(lows[-60:]), 0),
    }


def _volume_profile(bars: list[OHLCV], bins: int = 16) -> dict[str, Any]:
    lo = min(float(b.low) for b in bars)
    hi = max(float(b.high) for b in bars)
    close = float(bars[-1].close)
    total_vol = sum(max(0, int(b.volume)) for b in bars)
    if total_vol <= 0 or hi <= lo:
        return {
            "bins": [],
            "poc_price": _r(close, 0),
            "poc_volume_pct": None,
            "overhead_supply_pct": None,
            "below_support_pct": None,
            "close_vs_poc": "EQUAL",
        }

    step = (hi - lo) / bins
    buckets = [0.0] * bins
    for b in bars:
        vol = max(0, int(b.volume))
        if vol == 0:
            continue
        b_lo = max(0, min(bins - 1, int((float(b.low) - lo) / step)))
        b_hi = max(0, min(bins - 1, int((float(b.high) - lo) / step)))
        span = b_hi - b_lo + 1
        share = vol / span
        for idx in range(b_lo, b_hi + 1):
            buckets[idx] += share

    max_idx = max(range(bins), key=lambda idx: buckets[idx])
    poc_price = lo + (max_idx + 0.5) * step
    poc_pct = (buckets[max_idx] / total_vol) * 100.0
    overhead = sum(buckets[idx] for idx in range(bins) if (lo + (idx + 0.5) * step) > close)
    below = sum(buckets[idx] for idx in range(bins) if (lo + (idx + 0.5) * step) < close)
    profile_bins = [
        {"low": _r(lo + idx * step, 0), "high": _r(lo + (idx + 1) * step, 0),
         "mid": _r(lo + (idx + 0.5) * step, 0), "volume": _r(buckets[idx], 0),
         "pct": _r(buckets[idx] / total_vol * 100.0, 1)}
        for idx in range(bins)
    ]
    if close > poc_price * 1.005:
        close_vs_poc = "ABOVE_POC"
    elif close < poc_price * 0.995:
        close_vs_poc = "BELOW_POC"
    else:
        close_vs_poc = "AT_POC"

    return {
        "bins": profile_bins,
        "poc_price": _r(poc_price, 0),
        "poc_volume_pct": _r(poc_pct, 1),
        "overhead_supply_pct": _r(overhead / total_vol * 100.0, 1),
        "below_support_pct": _r(below / total_vol * 100.0, 1),
        "close_vs_poc": close_vs_poc,
    }


def _obv(closes: list[float], volumes: list[int]) -> dict[str, Any]:
    series = [0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            series.append(series[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            series.append(series[-1] - volumes[i])
        else:
            series.append(series[-1])
    if len(series) < 21:
        return {"trend_20d": "UNAVAILABLE"}
    delta = series[-1] - series[-21]
    avg_vol = sum(volumes[-20:]) / 20.0
    if avg_vol <= 0 or abs(delta) <= avg_vol * 0.5:
        trend = "FLAT"
    elif delta > 0:
        trend = "ACCUMULATION"
    else:
        trend = "DISTRIBUTION"
    return {"trend_20d": trend}


def _candle_pattern(bars: list[OHLCV]) -> dict[str, Any]:
    last = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    o, h, l, c = float(last.open), float(last.high), float(last.low), float(last.close)
    rng = h - l
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    pattern = "NORMAL"
    if rng <= 1e-9:
        pattern = "FLAT"
    elif prev is not None and float(prev.close) < float(prev.open) and c > o and o <= float(prev.close) and c >= float(prev.open):
        pattern = "BULLISH_ENGULFING"
    elif prev is not None and float(prev.close) > float(prev.open) and c < o and o >= float(prev.close) and c <= float(prev.open):
        pattern = "BEARISH_ENGULFING"
    elif lower_wick >= max(body * 2.0, rng * 0.55) and upper_wick <= rng * 0.15:
        pattern = "HAMMER"
    elif upper_wick >= max(body * 2.0, rng * 0.55) and lower_wick <= rng * 0.15:
        pattern = "SHOOTING_STAR"
    elif body <= rng * 0.12:
        pattern = "DOJI"
    elif body >= rng * 0.7 and c > o and (prev is None or (c / max(1e-9, float(prev.close)) - 1.0) >= 0.03):
        pattern = "LONG_BULL"
    elif body >= rng * 0.7 and c < o and (prev is None or (c / max(1e-9, float(prev.close)) - 1.0) <= -0.03):
        pattern = "LONG_BEAR"

    gap_pct = None
    if prev is not None and float(prev.close) > 0:
        gap_pct = ((o / float(prev.close)) - 1.0) * 100.0

    return {
        "pattern": pattern,
        "direction": "BULL" if c > o else "BEAR" if c < o else "FLAT",
        "gap_pct": _r(gap_pct, 2),
    }


def compute_full_chart_analysis(bars: list[OHLCV]) -> dict[str, Any]:
    """Compute multi-indicator chart analysis from sorted daily OHLCV bars."""
    ordered = sorted(bars, key=lambda b: b.trade_date)
    if len(ordered) < 61:
        raise ValueError("full chart analysis requires at least 61 daily bars")
    highs = [float(b.high) for b in ordered]
    lows = [float(b.low) for b in ordered]
    closes = [float(b.close) for b in ordered]
    volumes = [int(b.volume) for b in ordered]
    return {
        "bars_count": len(ordered),
        "session": ordered[-1].trade_date.isoformat(),
        "moving_averages": _moving_averages(closes),
        "bollinger": _bollinger(closes),
        "rsi14": _rsi(closes),
        "macd": _macd(closes),
        "ichimoku": _ichimoku(highs, lows, closes),
        "support_resistance": _support_resistance(highs, lows, closes),
        "volume_profile": _volume_profile(ordered),
        "obv": _obv(closes, volumes),
        "candle": _candle_pattern(ordered),
    }


def summarize_for_committee(analysis: dict[str, Any]) -> dict[str, Any]:
    """Strip time-series arrays so committee prompts stay compact and exact."""
    ma = analysis["moving_averages"]
    bb = analysis["bollinger"]
    rsi = analysis["rsi14"]
    macd = analysis["macd"]
    ichi = analysis["ichimoku"]
    sr = analysis["support_resistance"]
    vp = analysis["volume_profile"]
    return {
        "bars_count": analysis["bars_count"],
        "session": analysis["session"],
        "moving_averages": {k: ma[k] for k in ("sma5", "sma20", "sma60", "sma120", "alignment", "recent_cross", "disparity20_pct")},
        "bollinger_20_2": {k: bb[k] for k in ("upper", "middle", "lower", "percent_b", "bandwidth_pct", "squeeze", "state")},
        "rsi14": {k: rsi[k] for k in ("value", "zone", "divergence")},
        "macd_12_26_9": {k: macd[k] for k in ("macd", "signal", "histogram", "state")},
        "ichimoku": ichi,
        "support_resistance": sr,
        "volume_profile": {k: vp[k] for k in ("poc_price", "poc_volume_pct", "overhead_supply_pct", "below_support_pct", "close_vs_poc")},
        "obv": analysis["obv"],
        "candle": analysis["candle"],
    }
