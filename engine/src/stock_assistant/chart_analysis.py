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


def _extract_pivots(highs: list[float], lows: list[float], window: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    n = len(highs)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for i in range(window, n - window):
        if highs[i] == max(highs[i - window:i + window + 1]):
            if not swing_highs or i - swing_highs[-1][0] >= window:
                swing_highs.append((i, highs[i]))
            elif highs[i] > swing_highs[-1][1]:
                swing_highs[-1] = (i, highs[i])
        if lows[i] == min(lows[i - window:i + window + 1]):
            if not swing_lows or i - swing_lows[-1][0] >= window:
                swing_lows.append((i, lows[i]))
            elif lows[i] < swing_lows[-1][1]:
                swing_lows[-1] = (i, lows[i])
    return swing_highs, swing_lows


def _line_y(x1: int, y1: float, x2: int, y2: float, x: int) -> float:
    if x2 == x1:
        return y2
    return y1 + (y2 - y1) * (x - x1) / (x2 - x1)


def _detect_trendlines(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
) -> dict[str, Any]:
    n = len(closes)
    start_bound = max(0, n - 90)
    recent_lows = [p for p in swing_lows if p[0] >= start_bound]
    recent_highs = [p for p in swing_highs if p[0] >= start_bound]

    support_line = None
    best_sup_score = -1.0
    for i in range(len(recent_lows)):
        for j in range(i + 1, len(recent_lows)):
            x1, y1 = recent_lows[i]
            x2, y2 = recent_lows[j]
            if x2 - x1 < 8 or y2 < y1 * 0.995:
                continue
            # Reject lines heavily violated by intermediate closes.
            violations = 0
            for k in range(x1 + 1, n):
                ly = _line_y(x1, y1, x2, y2, k)
                if closes[k] < ly * 0.975:
                    violations += 1
            if violations > 2:
                continue
            cur_y = _line_y(x1, y1, x2, y2, n - 1)
            if cur_y <= 0 or cur_y > closes[-1] * 1.04:
                continue
            score = (x2 - x1) - violations * 10
            if score > best_sup_score:
                best_sup_score = score
                slope_pct = ((y2 / y1) - 1.0) * 100.0 / (x2 - x1) if y1 > 0 else 0.0
                support_line = {
                    "kind": "UPTREND_SUPPORT",
                    "x1": x1, "y1": _r(y1, 0),
                    "x2": n - 1, "y2": _r(cur_y, 0),
                    "anchor_x2": x2, "anchor_y2": _r(y2, 0),
                    "current_y": _r(cur_y, 0),
                    "slope_pct_per_day": _r(slope_pct, 3),
                }

    resistance_line = None
    best_res_score = -1.0
    for i in range(len(recent_highs)):
        for j in range(i + 1, len(recent_highs)):
            x1, y1 = recent_highs[i]
            x2, y2 = recent_highs[j]
            if x2 - x1 < 8 or y2 > y1 * 1.005:
                continue
            violations = 0
            for k in range(x1 + 1, n - 1):
                ly = _line_y(x1, y1, x2, y2, k)
                if closes[k] > ly * 1.025:
                    violations += 1
            if violations > 2:
                continue
            cur_y = _line_y(x1, y1, x2, y2, n - 1)
            if cur_y <= 0 or cur_y < closes[-1] * 0.85:
                continue
            score = (x2 - x1) - violations * 10
            if score > best_res_score:
                best_res_score = score
                slope_pct = ((y2 / y1) - 1.0) * 100.0 / (x2 - x1) if y1 > 0 else 0.0
                resistance_line = {
                    "kind": "DOWNTREND_RESISTANCE",
                    "x1": x1, "y1": _r(y1, 0),
                    "x2": n - 1, "y2": _r(cur_y, 0),
                    "anchor_x2": x2, "anchor_y2": _r(y2, 0),
                    "current_y": _r(cur_y, 0),
                    "slope_pct_per_day": _r(slope_pct, 3),
                    "broken_up": closes[-1] > cur_y * 1.005,
                }

    return {"support_line": support_line, "resistance_line": resistance_line}


def _detect_patterns(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
) -> dict[str, Any]:
    n = len(closes)
    close = closes[-1]
    detected: list[dict[str, Any]] = []

    # 1. Double Bottom (W-pattern)
    recent_lows = [p for p in swing_lows if p[0] >= n - 85]
    best_db = None
    for i in range(len(recent_lows)):
        for j in range(i + 1, len(recent_lows)):
            x1, y1 = recent_lows[i]
            x2, y2 = recent_lows[j]
            if not (10 <= x2 - x1 <= 55) or x2 < n - 30:
                continue
            if abs(y1 - y2) / max(y1, y2, 1e-9) > 0.038:
                continue
            mid_slice = highs[x1 + 1:x2]
            if not mid_slice:
                continue
            neck_val = max(mid_slice)
            neck_idx = x1 + 1 + mid_slice.index(neck_val)
            min_low = min(y1, y2)
            if (neck_val - min_low) / max(min_low, 1e-9) < 0.05:
                continue
            if close < min_low * 0.99:
                continue
            status = "BREAKOUT" if close >= neck_val * 0.995 else "FORMING"
            best_db = {
                "code": "DOUBLE_BOTTOM",
                "name_ko": "쌍바닥(W형)",
                "bias": "BULLISH",
                "status": status,
                "priority": 100 if status == "BREAKOUT" else 82,
                "neckline": _r(neck_val, 0),
                "breakout_price": _r(neck_val, 0),
                "support_price": _r(min_low, 0),
                "points": [[x1, _r(y1, 0)], [neck_idx, _r(neck_val, 0)], [x2, _r(y2, 0)]],
            }
    if best_db:
        detected.append(best_db)

    # 2. Double Top (M-pattern)
    recent_highs = [p for p in swing_highs if p[0] >= n - 85]
    best_dt = None
    for i in range(len(recent_highs)):
        for j in range(i + 1, len(recent_highs)):
            x1, y1 = recent_highs[i]
            x2, y2 = recent_highs[j]
            if not (10 <= x2 - x1 <= 55) or x2 < n - 25:
                continue
            if abs(y1 - y2) / max(y1, y2, 1e-9) > 0.038:
                continue
            mid_slice = lows[x1 + 1:x2]
            if not mid_slice:
                continue
            neck_val = min(mid_slice)
            neck_idx = x1 + 1 + mid_slice.index(neck_val)
            max_high = max(y1, y2)
            if (max_high - neck_val) / max(max_high, 1e-9) < 0.05:
                continue
            if close > max_high * 1.01:
                continue
            status = "BREAKDOWN" if close <= neck_val * 1.005 else "FORMING"
            best_dt = {
                "code": "DOUBLE_TOP",
                "name_ko": "쌍봉(M형)",
                "bias": "BEARISH",
                "status": status,
                "priority": 95 if status == "BREAKDOWN" else 76,
                "neckline": _r(neck_val, 0),
                "breakout_price": _r(max_high, 0),
                "support_price": _r(neck_val, 0),
                "points": [[x1, _r(y1, 0)], [neck_idx, _r(neck_val, 0)], [x2, _r(y2, 0)]],
            }
    if best_dt:
        detected.append(best_dt)

    # 3. Inverse Head & Shoulders
    if len(recent_lows) >= 3:
        best_ihs = None
        for i in range(len(recent_lows) - 2):
            ls_x, ls_y = recent_lows[i]
            h_x, h_y = recent_lows[i + 1]
            rs_x, rs_y = recent_lows[i + 2]
            if rs_x < n - 25 or rs_x - ls_x > 80:
                continue
            if not (h_y < ls_y * 0.96 and h_y < rs_y * 0.96):
                continue
            if abs(ls_y - rs_y) / max(ls_y, rs_y, 1e-9) > 0.055:
                continue
            p1_slice = highs[ls_x + 1:h_x]
            p2_slice = highs[h_x + 1:rs_x]
            if not p1_slice or not p2_slice:
                continue
            p1_val = max(p1_slice)
            p1_idx = ls_x + 1 + p1_slice.index(p1_val)
            p2_val = max(p2_slice)
            p2_idx = h_x + 1 + p2_slice.index(p2_val)
            neck = (p1_val + p2_val) / 2.0
            if (neck - h_y) / max(h_y, 1e-9) < 0.06:
                continue
            status = "BREAKOUT" if close >= neck * 0.995 else "FORMING"
            best_ihs = {
                "code": "INVERSE_HEAD_AND_SHOULDERS",
                "name_ko": "역헤드앤숄더",
                "bias": "BULLISH",
                "status": status,
                "priority": 105 if status == "BREAKOUT" else 85,
                "neckline": _r(neck, 0),
                "breakout_price": _r(neck, 0),
                "support_price": _r(rs_y, 0),
                "points": [
                    [ls_x, _r(ls_y, 0)], [p1_idx, _r(p1_val, 0)],
                    [h_x, _r(h_y, 0)], [p2_idx, _r(p2_val, 0)], [rs_x, _r(rs_y, 0)],
                ],
            }
        if best_ihs:
            detected.append(best_ihs)

    # 4. Cup & Handle (U-shaped cup + shallow right handle)
    if n >= 55:
        best_cup = None
        for cup_len in (90, 70, 50):
            if n < cup_len + 8:
                continue
            start = n - cup_len - 10
            # Left rim in first 25% of window
            l_end = start + max(8, cup_len // 4)
            left_rim = max(highs[start:l_end])
            left_idx = start + highs[start:l_end].index(left_rim)
            # Right rim before handle (last 5..18 bars are handle)
            r_start = n - 20
            r_end = n - 4
            if r_start <= left_idx + 15:
                continue
            right_rim = max(highs[r_start:r_end])
            right_idx = r_start + highs[r_start:r_end].index(right_rim)
            if abs(left_rim - right_rim) / max(left_rim, right_rim, 1e-9) > 0.06:
                continue
            cup_slice = lows[left_idx + 3:right_idx - 2]
            if len(cup_slice) < 12:
                continue
            cup_low = min(cup_slice)
            cup_low_idx = left_idx + 3 + cup_slice.index(cup_low)
            rim = max(left_rim, right_rim)
            depth = (rim - cup_low) / max(rim, 1e-9)
            if not (0.12 <= depth <= 0.45):
                continue
            # Check U-shape: low sits in central 60% of cup
            rel_pos = (cup_low_idx - left_idx) / max(1, right_idx - left_idx)
            if not (0.20 <= rel_pos <= 0.80):
                continue
            handle_low = min(lows[right_idx:n])
            handle_depth = (rim - handle_low) / max(rim, 1e-9)
            if handle_depth > depth * 0.50 or handle_depth < 0.01:
                continue
            status = "BREAKOUT" if close >= rim * 0.995 else "FORMING"
            best_cup = {
                "code": "CUP_AND_HANDLE",
                "name_ko": "컵앤핸들",
                "bias": "BULLISH",
                "status": status,
                "priority": 108 if status == "BREAKOUT" else 88,
                "rim_price": _r(rim, 0),
                "breakout_price": _r(rim, 0),
                "cup_low": _r(cup_low, 0),
                "handle_low": _r(handle_low, 0),
                "support_price": _r(handle_low, 0),
                "cup_start_idx": left_idx,
                "cup_low_idx": cup_low_idx,
                "handle_start_idx": right_idx,
                "end_idx": n - 1,
            }
            break
        if best_cup:
            detected.append(best_cup)

    # 5. Triangle / Ascending Triangle (converging highs & lows over last 25..60 bars)
    tri_highs = [p for p in swing_highs if p[0] >= n - 60]
    tri_lows = [p for p in swing_lows if p[0] >= n - 60]
    if len(tri_highs) >= 2 and len(tri_lows) >= 2:
        h1, h2 = tri_highs[0], tri_highs[-1]
        l1, l2 = tri_lows[0], tri_lows[-1]
        if h2[0] - h1[0] >= 12 and l2[0] - l1[0] >= 12:
            hi_slope = (h2[1] - h1[1]) / max(h1[1], 1e-9)
            lo_slope = (l2[1] - l1[1]) / max(l1[1], 1e-9)
            if lo_slope >= 0.025 and hi_slope <= 0.015:
                x_start = min(h1[0], l1[0])
                u_start = _line_y(h1[0], h1[1], h2[0], h2[1], x_start)
                l_start = _line_y(l1[0], l1[1], l2[0], l2[1], x_start)
                u_end = _line_y(h1[0], h1[1], h2[0], h2[1], n - 1)
                l_end = _line_y(l1[0], l1[1], l2[0], l2[1], n - 1)
                span_start = u_start - l_start
                span_end = u_end - l_end
                if span_start > 0 and 0 < span_end < span_start * 0.68:
                    is_asc = abs(hi_slope) <= 0.018
                    status = "BREAKOUT" if close > u_end * 1.005 else "FORMING"
                    detected.append({
                        "code": "TRIANGLE",
                        "subtype": "ASCENDING" if is_asc else "SYMMETRICAL",
                        "name_ko": "상승삼각형" if is_asc else "삼각수렴",
                        "bias": "BULLISH" if (is_asc or status == "BREAKOUT") else "NEUTRAL",
                        "status": status,
                        "priority": 96 if status == "BREAKOUT" else 78,
                        "breakout_price": _r(u_end, 0),
                        "support_price": _r(l_end, 0),
                        "upper_line": [x_start, _r(u_start, 0), n - 1, _r(u_end, 0)],
                        "lower_line": [x_start, _r(l_start, 0), n - 1, _r(l_end, 0)],
                    })

    # 6. Box Range / Breakout (tight horizontal channel over 25..45 bars prior to latest 3 bars)
    if n >= 35:
        for box_len in (40, 28):
            b_start = n - box_len - 3
            b_end = n - 3
            if b_start < 0:
                continue
            box_hi = max(highs[b_start:b_end])
            box_lo = min(lows[b_start:b_end])
            if box_lo <= 0:
                continue
            width_pct = (box_hi - box_lo) / box_lo
            if not (0.04 <= width_pct <= 0.17):
                continue
            top_touches = sum(1 for h in highs[b_start:b_end] if h >= box_hi * 0.985)
            bot_touches = sum(1 for l in lows[b_start:b_end] if l <= box_lo * 1.015)
            if top_touches >= 2 and bot_touches >= 2:
                if close > box_hi * 1.005:
                    status, bias, prio = "BREAKOUT", "BULLISH", 92
                elif close >= box_lo * 0.99:
                    status, bias, prio = "INSIDE", "NEUTRAL", 65
                else:
                    continue
                detected.append({
                    "code": "BOX_RANGE",
                    "name_ko": "박스권 돌파" if status == "BREAKOUT" else "박스권 횡보",
                    "bias": bias,
                    "status": status,
                    "priority": prio,
                    "top": _r(box_hi, 0),
                    "bottom": _r(box_lo, 0),
                    "breakout_price": _r(box_hi, 0),
                    "support_price": _r(box_lo, 0),
                    "start_idx": b_start,
                    "end_idx": n - 1,
                })
                break

    detected.sort(key=lambda item: item["priority"], reverse=True)
    primary = detected[0] if detected else None
    return {"primary": primary, "detected": detected[:3]}


GRADE_LABELS_KO = {
    "ENTRY_READY": "🟢 진입 검토",
    "WAIT_BREAKOUT": "🟡 돌파 대기",
    "WAIT_PULLBACK": "🟠 눌림 대기(과열)",
    "CHART_CAUTION": "🔴 차트 주의",
}


def _build_trade_plan(
    close: float,
    ma: dict[str, Any],
    bb: dict[str, Any],
    rsi: dict[str, Any],
    macd: dict[str, Any],
    ichi: dict[str, Any],
    sr: dict[str, Any],
    vp: dict[str, Any],
    patterns: dict[str, Any],
    volume_ratio: float | None,
) -> dict[str, Any]:
    primary = patterns.get("primary")
    sma20 = ma.get("sma20")
    near_sup = sr.get("nearest_support")
    near_res = sr.get("nearest_resistance")

    # Determine key breakout / support / stop-loss prices deterministically.
    breakout_price = None
    if primary and primary.get("bias") != "BEARISH" and primary.get("breakout_price"):
        breakout_price = float(primary["breakout_price"])
    elif near_res is not None:
        breakout_price = float(near_res)
    elif sr.get("high_60d") is not None:
        breakout_price = float(sr["high_60d"])

    support_candidates = [float(x) for x in (near_sup, sma20) if x is not None and float(x) < close * 1.005]
    support_price = max(support_candidates) if support_candidates else (float(near_sup) if near_sup else (float(sma20) if sma20 else None))
    # Stop-loss / invalidation defaults to SMA20 when below close, else nearest support.
    if sma20 is not None and float(sma20) < close:
        stop_loss = float(sma20)
    elif near_sup is not None and float(near_sup) < close:
        stop_loss = float(near_sup)
    else:
        stop_loss = float(sma20) if sma20 is not None else None

    breakout_pct = ((breakout_price / close) - 1.0) * 100.0 if breakout_price and close > 0 else None
    support_pct = ((support_price / close) - 1.0) * 100.0 if support_price and close > 0 else None
    stop_pct = ((stop_loss / close) - 1.0) * 100.0 if stop_loss and close > 0 else None

    rsi_val = rsi.get("value")
    pct_b = bb.get("percent_b")
    disp20 = ma.get("disparity20_pct")
    overhead = vp.get("overhead_supply_pct")
    caution_pattern = bool(primary and primary.get("bias") == "BEARISH")
    bullish_breakout = bool(primary and primary.get("bias") == "BULLISH" and primary.get("status") == "BREAKOUT")
    forming_pattern = bool(primary and primary.get("bias") == "BULLISH" and primary.get("status") == "FORMING")

    reasons: list[str] = []
    annotations: list[str] = []

    if primary:
        tag = f"{primary['name_ko']} {'돌파' if primary['status'] == 'BREAKOUT' else '이탈' if primary['status'] == 'BREAKDOWN' else '형성'}"
        annotations.append(tag)
    if rsi_val is not None and rsi_val >= 70.0:
        annotations.append(f"과열 RSI {rsi_val:.1f}")
    elif rsi.get("divergence") == "BEARISH_DIVERGENCE":
        annotations.append("RSI 약세 다이버전스")
    elif rsi.get("divergence") == "BULLISH_DIVERGENCE":
        annotations.append("RSI 상승 다이버전스")
    if overhead is not None and overhead >= 60.0:
        annotations.append(f"상단 매물벽 {overhead:.0f}%")
    if ma.get("recent_cross", "NONE") != "NONE":
        cross_ko = {"GOLDEN_20_60": "20/60 골든크로스", "DEAD_20_60": "20/60 데드크로스",
                    "GOLDEN_5_20": "5/20 골든크로스", "DEAD_5_20": "5/20 데드크로스"}
        annotations.append(cross_ko.get(ma["recent_cross"], ma["recent_cross"]))
    elif bb.get("squeeze"):
        annotations.append("볼린저 스퀴즈")

    # Classify execution grade (strict priority order).
    if caution_pattern or ma.get("alignment") == "BEARISH_FULL" or (sma20 is not None and close < float(sma20) * 0.995):
        grade = "CHART_CAUTION"
        if caution_pattern:
            reasons.append(f"{primary['name_ko']} 약세 신호")
        if sma20 is not None and close < float(sma20):
            reasons.append("20일선 하회")
        if ma.get("alignment") == "BEARISH_FULL":
            reasons.append("이평 완전 역배열")
    elif (rsi_val is not None and rsi_val >= 70.0) or (pct_b is not None and pct_b > 1.0) or (disp20 is not None and disp20 >= 12.0):
        grade = "WAIT_PULLBACK"
        if rsi_val is not None and rsi_val >= 70.0:
            reasons.append(f"RSI {rsi_val:.1f} 과매수")
        if pct_b is not None and pct_b > 1.0:
            reasons.append("볼린저 상단 초과")
        if disp20 is not None and disp20 >= 12.0:
            reasons.append(f"20일 이격 +{disp20:.1f}%")
    elif (
        (overhead is not None and overhead >= 60.0)
        or (forming_pattern and breakout_pct is not None and breakout_pct > 0.3)
        or (breakout_pct is not None and 0.3 <= breakout_pct <= 4.5)
        or (volume_ratio is not None and volume_ratio < 0.55)
        or ichi.get("cloud_position") == "BELOW_CLOUD"
    ):
        grade = "WAIT_BREAKOUT"
        if overhead is not None and overhead >= 60.0:
            reasons.append(f"상단 매물 {overhead:.0f}% 저항")
        if breakout_pct is not None and 0.3 <= breakout_pct <= 4.5 and breakout_price:
            reasons.append(f"저항 {breakout_price:,.0f}원 직전")
        if volume_ratio is not None and volume_ratio < 0.55:
            reasons.append(f"거래량 약세({volume_ratio:.2f}배)")
        if ichi.get("cloud_position") == "BELOW_CLOUD":
            reasons.append("일목 구름 아래")
    else:
        grade = "ENTRY_READY"
        if bullish_breakout:
            reasons.append(f"{primary['name_ko']} 돌파")
        elif ma.get("alignment") in {"BULLISH_FULL", "BULLISH_MID"}:
            reasons.append("이평 정배열·비과열")
        else:
            reasons.append("20일선 지지·비과열")

    bonus_map = {
        "ENTRY_READY": 0.25,
        "WAIT_BREAKOUT": 0.0,
        "WAIT_PULLBACK": -0.20,
        "CHART_CAUTION": -0.35,
    }
    bonus = bonus_map[grade] + (0.15 if bullish_breakout else 0.05 if forming_pattern else 0.0)

    return {
        "grade": grade,
        "grade_label": GRADE_LABELS_KO[grade],
        "reasons": reasons[:3],
        "annotations": annotations[:3],
        "breakout_price": _r(breakout_price, 0),
        "breakout_pct": _r(breakout_pct, 2),
        "support_price": _r(support_price, 0),
        "support_pct": _r(support_pct, 2),
        "stop_loss": _r(stop_loss, 0),
        "stop_loss_pct": _r(stop_pct, 2),
        "scoring_bonus": _r(bonus, 3),
    }


def compute_full_chart_analysis(bars: list[OHLCV]) -> dict[str, Any]:
    """Compute multi-indicator chart analysis, 6 classic patterns, trendlines, and trade plan."""
    ordered = sorted(bars, key=lambda b: b.trade_date)
    if len(ordered) < 61:
        raise ValueError("full chart analysis requires at least 61 daily bars")
    highs = [float(b.high) for b in ordered]
    lows = [float(b.low) for b in ordered]
    closes = [float(b.close) for b in ordered]
    volumes = [int(b.volume) for b in ordered]
    prior20_avg = sum(volumes[-21:-1]) / 20.0
    volume_ratio = (volumes[-1] / prior20_avg) if prior20_avg > 0 else None

    ma = _moving_averages(closes)
    bb = _bollinger(closes)
    rsi = _rsi(closes)
    macd = _macd(closes)
    ichi = _ichimoku(highs, lows, closes)
    sr = _support_resistance(highs, lows, closes)
    vp = _volume_profile(ordered)
    obv = _obv(closes, volumes)
    candle = _candle_pattern(ordered)
    swing_highs, swing_lows = _extract_pivots(highs, lows)
    trendlines = _detect_trendlines(highs, lows, closes, swing_highs, swing_lows)
    patterns = _detect_patterns(highs, lows, closes, swing_highs, swing_lows)
    trade_plan = _build_trade_plan(
        closes[-1], ma, bb, rsi, macd, ichi, sr, vp, patterns, volume_ratio,
    )
    return {
        "bars_count": len(ordered),
        "session": ordered[-1].trade_date.isoformat(),
        "moving_averages": ma,
        "bollinger": bb,
        "rsi14": rsi,
        "macd": macd,
        "ichimoku": ichi,
        "support_resistance": sr,
        "volume_profile": vp,
        "obv": obv,
        "candle": candle,
        "trendlines": trendlines,
        "patterns": patterns,
        "trade_plan": trade_plan,
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
    patterns = analysis.get("patterns") or {}
    return {
        "bars_count": analysis["bars_count"],
        "session": analysis["session"],
        "trade_plan": analysis.get("trade_plan"),
        "primary_pattern": patterns.get("primary"),
        "detected_patterns": [
            {k: p[k] for k in ("code", "name_ko", "bias", "status", "breakout_price", "support_price") if k in p}
            for p in patterns.get("detected", [])
        ],
        "trendlines": analysis.get("trendlines"),
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
