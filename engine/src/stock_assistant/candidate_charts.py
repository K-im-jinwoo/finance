"""Read-only PNG attachments for a unified report's final candidates only.

Run with the host Hermes Python (which already provides Pillow), not inside the
dependency-free engine container. No market fetches, report updates or sends.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from .chart_analysis import compute_full_chart_analysis, summarize_for_committee
from .indicators import price_features, simple_average
from .models import to_json_value
from .research_data import APPLICATION_ID
from .research_repository import ReadOnlyResearchRepository
from .validation import validate_analysis_time


VERSION = "CANDIDATE_CHARTS_V1"
RENDER_VERSION = "2026-11-19.1"
KST = timezone(timedelta(hours=9))
FINAL_DECISIONS = {"BUY_RECOMMENDATION", "WATCH"}
REPORT_ID = re.compile(r"UR-[a-f0-9]{24}")
LABELS = {"BUY_RECOMMENDATION": "다음 거래일 조건부 추천", "WATCH": "관찰"}


class ChartUnavailable(ValueError):
    pass


def _label(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value)).replace("MEDIA:", "MEDIA :")[:100]


def _fingerprint(bars):
    return hashlib.sha256(json.dumps(to_json_value(bars), sort_keys=True).encode()).hexdigest()


def technical_analysis(data):
    """Explain only the verified chart inputs; never create a new trade verdict."""
    bars, metrics = data["bars"], data["metrics"]
    last = bars[-1]
    close, sma20, sma60, atr = (Decimal(metrics[k]) for k in ("close", "sma20", "sma60", "atr14"))
    def relation(a, b):
        return "ABOVE" if a > b else "BELOW" if a < b else "EQUAL"
    positions = {"ABOVE": "위", "BELOW": "아래", "EQUAL": "동일"}
    versus20, versus60, order = relation(close, sma20), relation(close, sma60), relation(sma20, sma60)
    avg_volume = simple_average([Decimal(b.volume) for b in bars[-21:-1]])
    multiple = Decimal(last.volume) / avg_volume if avg_volume > 0 else None
    candle = "양봉" if last.close > last.open else "음봉" if last.close < last.open else "시가·종가 동일"
    methods = [
        {"code": "OHLC_CANDLE", "name": "일봉 캔들", "purpose": "하루 가격 범위와 시가 대비 종가 확인",
         "calculation": "같은 거래일의 시가·고가·저가·종가를 표시",
         "values": {k: str(getattr(last, k)) for k in ("open", "high", "low", "close")},
         "reading": candle, "limit": "한 봉만으로 반등·추세 전환이나 캔들 패턴을 확정하지 않음"},
        {"code": "SMA20_60", "name": "20·60거래일 단순이동평균", "purpose": "가격 위치와 두 평균선의 배열 비교",
         "calculation": "각각 최근 20·60거래일 종가의 산술평균",
         "values": {"close": str(close), "sma20": str(sma20), "sma60": str(sma60),
                    "close_vs_sma20": versus20, "close_vs_sma60": versus60, "sma20_vs_sma60": order},
         "reading": f"종가: 20일선 대비 {positions[versus20]}, 60일선 대비 {positions[versus60]} · "
                    f"20일선: 60일선 대비 {positions[order]}",
         "limit": "현재 배열만으로 골든크로스 발생·추세 지속·매수 시점을 확정하지 않음"},
        {"code": "VOLUME_RATIO20", "name": "직전 20거래일 대비 거래량", "purpose": "평소 거래량과 참여 규모 비교",
         "calculation": "해당 일봉 거래량 ÷ 해당 봉을 제외한 직전 20거래일 평균 거래량",
         "values": {"volume_shares": last.volume, "prior20_average_shares": str(avg_volume),
                    "multiple": str(multiple) if multiple is not None else None},
         "reading": f"평균 대비 {multiple:.2f}배" if multiple is not None else "직전 평균이 0주여서 배율 확인 불가",
         "limit": "거래량만으로 매수·매도 주체나 기관 자금 유입을 판별하지 않음"},
        {"code": "WILDER_ATR14", "name": "Wilder 방식 ATR14", "purpose": "전일 종가와의 갭을 반영한 변동폭 확인",
         "calculation": "TR=max(고가-저가, |고가-전일종가|, |저가-전일종가|); "
                        "초기 14개 TR 평균 뒤 (이전 ATR×13+현재 TR)÷14 · 기존 엔진의 최근 61개 일봉 사용",
         "values": {"atr_krw": str(atr), "atr_pct_of_close": str(atr / close * 100)},
         "reading": f"ATR {atr:,.2f}원 · 종가 대비 {atr / close * 100:.2f}%",
         "limit": "변동성 지표이며 상승·하락 방향, 목표가·손절가를 단독으로 정하지 않음"},
    ]
    patterns = {"BOTTOM_REBOUND": "저점 반등형", "MOMENTUM_CONTINUATION": "모멘텀 지속형",
                "WAIT_FOR_SETUP": "진입 모양 확인 대기"}
    strategy = data.get("stored_strategy")
    if not isinstance(strategy, str):
        strategy = None
    return {"kind": "DETERMINISTIC_READOUT", "session": data["session"].isoformat(),
            "methods": methods,
            "stored_pattern": {"code": strategy, "name": patterns[strategy]} if strategy in patterns else None,
            "limits": ["저장 일봉 기준의 판독이며 현재 시세·테마 사이클·실제 진입 조건은 별도 확인 필요"],
            "new_trade_decision": False}


def explain_technical_analysis(analysis):
    """User-facing methods, purpose, measured values and bounded interpretation."""
    methods = {m["code"]: m for m in analysis["methods"]}
    candle, trend, volume, atr = (methods[k] for k in ("OHLC_CANDLE", "SMA20_60", "VOLUME_RATIO20", "WILDER_ATR14"))
    c, t, v = candle["values"], trend["values"], volume["values"]
    lines = ["사용 기법과 판독 — 저장 일봉 기준",
             f"• 일봉 캔들: 하루 가격 범위 확인 · 시가 {Decimal(c['open']):,.0f} / 고가 {Decimal(c['high']):,.0f} / "
             f"저가 {Decimal(c['low']):,.0f} / 종가 {Decimal(c['close']):,.0f}원 · {candle['reading']}",
             f"• SMA20/60: 최근 20·60거래일 평균 종가로 추세 참고 · 20일선 {Decimal(t['sma20']):,.2f} / "
             f"60일선 {Decimal(t['sma60']):,.2f}원 · {trend['reading']}",
             f"• 거래량 비교: 당일 {v['volume_shares']:,}주 ÷ 직전 20일 평균 {Decimal(v['prior20_average_shares']):,.2f}주 · "
             + volume["reading"],
             f"• Wilder ATR14: 갭을 포함한 변동폭 확인 · {atr['reading']} · 가격 방향을 나타내는 값은 아님"]
    if analysis["stored_pattern"]:
        lines.append("저장된 가격 패턴: " + analysis["stored_pattern"]["name"] + " · 기존 분류 유지")
    lines.append("판독 한계: 평균선 배열만으로 골든크로스를, 거래량만으로 매수세를 확정하지 않습니다. "
                 + analysis["limits"][0])
    return "\n".join(lines)


def load_report(path: Path, report_id: str, *, source_database: Path):
    """Select the exact saved report in SQL, without loading archive history."""
    if not REPORT_ID.fullmatch(report_id):
        raise ValueError("INVALID_REPORT_ID")
    path = path.resolve(strict=True)
    if path == source_database.resolve(strict=True) or path.samefile(source_database):
        raise ValueError("RESEARCH_DATABASE_MUST_DIFFER")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("INVALID_RESEARCH_DATABASE")
        row = db.execute("SELECT payload_json FROM research_inputs WHERE kind='unified_report' AND symbol='ALL' "
                         "AND json_extract(payload_json,'$.report_id')=? AND julianday(observed_at)<=julianday(?) "
                         "ORDER BY julianday(observed_at) DESC LIMIT 1",
                         (report_id, datetime.now(timezone.utc).isoformat())).fetchone()
    if row is None:
        raise ValueError("EXACT_REPORT_UNAVAILABLE")
    return json.loads(row[0])


def prepare_chart(report, candidate, repository):
    """Bind chart bars and deterministic features to the stored report inputs."""
    symbol = candidate.get("symbol", "")
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise ChartUnavailable("INVALID_SYMBOL")
    if candidate.get("action_decision") not in FINAL_DECISIONS:
        raise ChartUnavailable("NOT_A_FINAL_CANDIDATE")
    as_of = validate_analysis_time(datetime.fromisoformat(report["as_of"]))
    price = candidate.get("price", {})
    if price.get("status") not in {"READY", "WATCH"} or not price.get("session"):
        raise ChartUnavailable("PRICE_NOT_VERIFIED")
    session = date.fromisoformat(price["session"])
    if session > as_of.astimezone(KST).date():
        raise ChartUnavailable("FUTURE_PRICE_SESSION")
    bars = repository.bars_for(symbol, as_of=as_of, limit=120)
    if len(bars) < 61:
        raise ChartUnavailable("PRICE_HISTORY_UNAVAILABLE")
    if any(b.symbol != symbol or b.trade_date > session or b.observed_at > as_of
           or min(b.open, b.high, b.low, b.close) <= 0 for b in bars):
        raise ChartUnavailable("INVALID_POINT_IN_TIME_HISTORY")
    if len({b.trade_date for b in bars}) != len(bars):
        raise ChartUnavailable("DUPLICATE_PRICE_SESSION")
    bars = sorted(bars, key=lambda b: b.trade_date)
    if bars[-1].trade_date != session:
        raise ChartUnavailable("PRICE_SESSION_INCOMPLETE")
    fingerprint = _fingerprint(bars)
    if fingerprint != candidate.get("price_input_fingerprint"):
        raise ChartUnavailable("REPORT_PRICE_INPUT_MISMATCH")
    features = to_json_value(price_features(bars))
    saved = price.get("metrics", {})
    for key in ("close", "sma20", "sma60", "atr14", "volume_ratio20"):
        if key not in saved or Decimal(str(saved[key])) != Decimal(features[key]):
            raise ChartUnavailable("REPORT_TECHNICAL_METRIC_MISMATCH")
    closes = [b.close for b in bars]
    averages = {period: [None if i + 1 < period else simple_average(closes[i + 1 - period:i + 1])
                         for i in range(len(bars))] for period in (20, 60)}
    data = {"report_id": report["report_id"], "as_of": as_of, "symbol": symbol,
            "name": _label(candidate.get("name", symbol)), "decision": candidate["action_decision"],
            "session": session, "bars": bars, "averages": averages, "metrics": features,
            "stored_strategy": price.get("strategy"),
            "price_input_fingerprint": fingerprint, "sources": sorted({b.source for b in bars})}
    data["technical_analysis"] = technical_analysis(data)
    data["extended_analysis"] = compute_full_chart_analysis(bars)
    return data


def _font_file(explicit=None):
    candidates = [explicit or os.getenv("STOCK_CHART_FONT_FILE"),
                  "C:/Windows/Fonts/malgun.ttf",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for name in candidates:
        if name and Path(name).is_file():
            return str(name), any(word in str(name).lower() for word in ("malgun", "notosanscjk", "nanum"))
    return None, False


def _draw_dashed_hline(draw, x0: float, x1: float, y: float, fill: str, *, dash: int = 8, gap: int = 6, width: int = 1):
    x = x0
    while x < x1:
        draw.line((x, y, min(x1, x + dash), y), fill=fill, width=width)
        x += dash + gap


def _draw_series_line(draw, values, px_fn, py_fn, fill: str, width: int = 2):
    segment = []
    for idx, val in enumerate(values):
        if val is None:
            if len(segment) > 1:
                draw.line(segment, fill=fill, width=width)
            segment = []
        else:
            segment.append((px_fn(idx), py_fn(float(val))))
    if len(segment) > 1:
        draw.line(segment, fill=fill, width=width)


def build_committee_chart_sheet(chart_paths: list[Path], target: Path) -> Path:
    """Combine up to 5 candidate PNGs into one contact sheet for single-image vision review."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise ChartUnavailable("PNG_LIBRARY_UNAVAILABLE") from exc
    valid = [p for p in chart_paths if p.is_file() and not p.is_symlink()][:5]
    if not valid:
        raise ChartUnavailable("NO_CHART_IMAGES")
    if len(valid) == 1:
        tile_w, tile_h, cols = 1400, 980, 1
    else:
        tile_w, tile_h, cols = 1000, 700, 2
    rows = (len(valid) + cols - 1) // cols
    pad = 16
    sheet = Image.new("RGB", (cols * tile_w + (cols + 1) * pad, rows * tile_h + (rows + 1) * pad), "#080f1d")
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
    for idx, path in enumerate(valid):
        with Image.open(path) as img:
            tile = img.convert("RGB").resize((tile_w, tile_h), resample)
        r, c = divmod(idx, cols)
        sheet.paste(tile, (pad + c * (tile_w + pad), pad + r * (tile_h + pad)))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ChartUnavailable("UNSAFE_OUTPUT_PATH")
    fd, temporary = tempfile.mkstemp(prefix=".sheet-", suffix=".png", dir=target.parent)
    try:
        os.close(fd)
        sheet.save(temporary, format="PNG")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        sheet.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def render_chart(data, target: Path, *, font_file=None, demo=False):
    """Draw multi-panel technical chart (OHLC+MA5/20/60/120+BB+S/R+VP / Volume / RSI / MACD)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ChartUnavailable("PNG_LIBRARY_UNAVAILABLE") from exc
    font_path, korean = _font_file(font_file)
    font = lambda size: ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default(size=size)
    image = Image.new("RGB", (1600, 1120), "#0b1324")
    draw = ImageDraw.Draw(image)
    ink, muted, grid = "#e9eff8", "#9cadc7", "#233148"
    up, down = "#ef6874", "#64aaff"
    ma5_c, ma20_c, ma60_c, ma120_c = "#f7a8b8", "#ffcc72", "#68dbc8", "#b794f6"
    bb_c, sup_c, res_c, poc_c = "#4e6a94", "#4fd18b", "#f87171", "#f6c453"
    rsi_c, macd_c, sig_c = "#90cdf4", "#63b3ed", "#f6ad55"
    words = (lambda ko, en: ko if korean else en)

    ext = data.get("extended_analysis") or compute_full_chart_analysis(data["bars"])
    ma_info = ext["moving_averages"]
    bb_info = ext["bollinger"]
    rsi_info = ext["rsi14"]
    macd_info = ext["macd"]
    ichi_info = ext["ichimoku"]
    sr_info = ext["support_resistance"]
    vp_info = ext["volume_profile"]
    obv_info = ext["obv"]
    candle_info = ext["candle"]

    # Header
    title = f"{data['name']}  {data['symbol']}" if korean else data["symbol"]
    draw.text((60, 22), title, font=font(34), fill=ink)
    status = LABELS[data["decision"]] if korean else ("WATCH" if data["decision"] == "WATCH" else "NEXT SESSION: CONDITIONAL")
    draw.text((60, 68), status, font=font(20), fill=ma20_c)
    if demo:
        draw.text((760, 32), words("표시 예시 · 가상 데이터 · 추천 아님", "DEMO / SYNTHETIC / NOT A RECOMMENDATION"),
                  font=font(22), fill=up)
    draw.text((60, 98), words("일봉 기준일", "DAILY CLOSE") + f" {data['session']}   |   "
              + words("보고서 시각", "REPORT AS OF") + " " + data["as_of"].astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
              font=font(17), fill=muted)

    # Summary cards (4 compact cards)
    m = data["metrics"]
    align_kr = {"BULLISH_FULL": "완전 정배열", "BULLISH_MID": "중기 정배열",
                "BEARISH_FULL": "완전 역배열", "BEARISH_MID": "중기 역배열", "MIXED": "혼조"}
    cloud_kr = {"ABOVE_CLOUD": "구름 위", "IN_CLOUD": "구름 안", "BELOW_CLOUD": "구름 아래", "UNAVAILABLE": "-"}
    macd_short = {
        "BULLISH_CROSS": "골든크로스", "BEARISH_CROSS": "데드크로스",
        "ABOVE_SIGNAL_EXPANDING": "상승확대", "ABOVE_SIGNAL_CONTRACTING": "상승둔화",
        "BELOW_SIGNAL_IMPROVING": "반등시도", "BELOW_SIGNAL_WEAKENING": "하락우위",
    }
    rsi_str = f"{rsi_info['value']:.1f}" if rsi_info["value"] is not None else "N/A"
    bb_str = f"%B {bb_info['percent_b']:.2f}" if bb_info["percent_b"] is not None else "%B N/A"
    poc_str = f"{vp_info['poc_price']:,.0f}" if vp_info.get("poc_price") is not None else "N/A"
    ov_str = f"{vp_info['overhead_supply_pct']:.0f}%" if vp_info.get("overhead_supply_pct") is not None else "N/A"
    cards = [
        (words("종가 · 이평배열", "CLOSE / MA ALIGN"),
         f"{Decimal(m['close']):,.0f}  {words(align_kr.get(ma_info['alignment'], '혼조'), ma_info['alignment'])}"),
        (words("RSI(14) · MACD", "RSI14 / MACD"),
         f"RSI {rsi_str}  |  {words(macd_short.get(macd_info['state'], '-'), macd_info['state'][:10])}"),
        (words("볼린저(20,2σ) · 일목", "BOLLINGER / CLOUD"),
         f"{bb_str}  |  {words(cloud_kr.get(ichi_info['cloud_position'], '-'), ichi_info['cloud_position'])}"),
        (words("매물대 POC · 상단매물", "POC / OVERHEAD"),
         f"{poc_str}  |  {words('위 ', 'UP ')}{ov_str}"),
    ]
    for i, (label, value) in enumerate(cards):
        x = 60 + i * 374
        draw.rounded_rectangle((x, 132, x + 358, 214), radius=10, fill="#14213a")
        draw.text((x + 14, 142), label, font=font(15), fill=muted)
        draw.text((x + 14, 170), value, font=font(22), fill=ink)

    # Common horizontal coordinate system for all 4 aligned panels
    left, right = 82, 1310
    vp_left, vp_right = 1322, 1456
    bars = data["bars"]
    px = lambda i: left + (i + 0.5) * (right - left) / len(bars)
    width = max(2, int((right - left) / len(bars) * 0.58))

    # Panel 1: Main price + MA5/20/60/120 + Bollinger + Support/Resistance + Volume Profile
    p1_top, p1_bot = 262, 654
    draw.rounded_rectangle((60, 228, 1540, 668), radius=12, fill="#101c30")
    price_vals = [float(b.low) for b in bars] + [float(b.high) for b in bars]
    for s in (bb_info["series"]["upper"], bb_info["series"]["lower"]):
        price_vals.extend(float(v) for v in s if v is not None)
    lo, hi = min(price_vals), max(price_vals)
    padding = max((hi - lo) * 0.06, hi * 0.015, 1.0)
    lo, hi = lo - padding, hi + padding
    py1 = lambda val: p1_bot - (float(val) - lo) / (hi - lo) * (p1_bot - p1_top)

    for i in range(6):
        val = lo + (hi - lo) * i / 5
        y = py1(val)
        draw.line((left, y, right, y), fill=grid, width=1)
        draw.text((vp_right + 10, y - 8), f"{val:,.0f}", font=font(14), fill=muted)

    # Volume Profile horizontal bars on right side of Panel 1
    vp_bins = vp_info.get("bins") or []
    max_vp = max((float(b["volume"]) for b in vp_bins), default=0.0)
    if max_vp > 0:
        for b in vp_bins:
            y_hi = py1(float(b["high"]))
            y_lo = py1(float(b["low"]))
            bar_w = (float(b["volume"]) / max_vp) * (vp_right - vp_left)
            is_poc = vp_info.get("poc_price") is not None and abs(float(b["mid"]) - float(vp_info["poc_price"])) <= (hi - lo) / 30.0
            fill_c = poc_c if is_poc else "#2a3f63"
            draw.rectangle((vp_left, min(y_hi, y_lo) + 1, vp_left + max(2.0, bar_w), max(y_hi, y_lo) - 1), fill=fill_c)
        if vp_info.get("poc_price") is not None:
            poc_y = py1(float(vp_info["poc_price"]))
            _draw_dashed_hline(draw, left, vp_right, poc_y, poc_c, dash=6, gap=6, width=1)
            draw.text((vp_left + 2, max(p1_top, poc_y - 16)), "POC", font=font(13), fill=poc_c)

    # Support & Resistance horizontal lines
    for level in sr_info.get("resistances") or []:
        if lo <= float(level) <= hi:
            y = py1(float(level))
            _draw_dashed_hline(draw, left, right, y, res_c, dash=8, gap=6, width=1)
            draw.text((left + 4, max(p1_top, y - 16)), f"R {float(level):,.0f}", font=font(13), fill=res_c)
    for level in sr_info.get("supports") or []:
        if lo <= float(level) <= hi:
            y = py1(float(level))
            _draw_dashed_hline(draw, left, right, y, sup_c, dash=8, gap=6, width=1)
            draw.text((left + 4, min(p1_bot - 16, y + 2)), f"S {float(level):,.0f}", font=font(13), fill=sup_c)

    # Bollinger Bands (20, 2std)
    _draw_series_line(draw, bb_info["series"]["upper"], px, py1, bb_c, width=1)
    _draw_series_line(draw, bb_info["series"]["lower"], px, py1, bb_c, width=1)

    # Candles
    for i, b in enumerate(bars):
        x = px(i)
        color = up if b.close > b.open else down if b.close < b.open else muted
        draw.line((x, py1(b.high), x, py1(b.low)), fill=color, width=2)
        y1, y2 = sorted((py1(b.open), py1(b.close)))
        draw.rectangle((x - width / 2, y1, x + width / 2, max(y1 + 2, y2)), fill=color)

    # Moving averages: SMA 5 / 20 / 60 / 120
    for period, color, lw in ((5, ma5_c, 1), (20, ma20_c, 2), (60, ma60_c, 2), (120, ma120_c, 2)):
        _draw_series_line(draw, ma_info["series"][period], px, py1, color, width=lw)

    # Panel 1 Legend
    draw.text((84, 234), "MA5", font=font(14), fill=ma5_c)
    draw.text((135, 234), "MA20", font=font(14), fill=ma20_c)
    draw.text((198, 234), "MA60", font=font(14), fill=ma60_c)
    draw.text((260, 234), "MA120", font=font(14), fill=ma120_c)
    draw.text((332, 234), "BB(20,2)", font=font(14), fill=bb_c)
    draw.text((422, 234), words("지지(S)", "SUP"), font=font(14), fill=sup_c)
    draw.text((490, 234), words("저항(R)", "RES"), font=font(14), fill=res_c)
    draw.text((558, 234), words("매물대(VP/POC)", "VOL PROFILE"), font=font(14), fill=poc_c)

    # Panel 2: Volume
    p2_top, p2_bot = 702, 770
    draw.rounded_rectangle((60, 676, 1540, 778), radius=10, fill="#101c30")
    max_volume = max((b.volume for b in bars), default=0)
    for i, b in enumerate(bars):
        x = px(i)
        color = up if b.close > b.open else down if b.close < b.open else muted
        h = (b.volume / max(1, max_volume)) * (p2_bot - p2_top)
        draw.rectangle((x - width / 2, p2_bot - h, x + width / 2, p2_bot), fill=color)
    obv_kr = {"ACCUMULATION": "매집(상승)", "DISTRIBUTION": "분산(하락)", "FLAT": "중립", "UNAVAILABLE": "-"}
    draw.text((84, 681), words("거래량", "VOLUME") + f" max {max_volume:,}   |   OBV(20d): "
              + words(obv_kr.get(obv_info["trend_20d"], "-"), obv_info["trend_20d"]), font=font(14), fill=muted)

    # Panel 3: RSI(14)
    p3_top, p3_bot = 808, 886
    draw.rounded_rectangle((60, 786, 1540, 896), radius=10, fill="#101c30")
    py3 = lambda val: p3_bot - (max(0.0, min(100.0, float(val))) / 100.0) * (p3_bot - p3_top)
    for lvl, col in ((70, res_c), (50, grid), (30, sup_c)):
        y = py3(lvl)
        _draw_dashed_hline(draw, left, right, y, col, dash=6, gap=6, width=1)
        draw.text((right + 12, y - 7), str(lvl), font=font(13), fill=col if lvl != 50 else muted)
    _draw_series_line(draw, rsi_info["series"], px, py3, rsi_c, width=2)
    div_tag = f"  [{rsi_info['divergence']}]" if rsi_info["divergence"] != "NONE" else ""
    draw.text((84, 790), f"RSI(14): {rsi_str} ({rsi_info['zone']}){div_tag}", font=font(14), fill=rsi_c)

    # Panel 4: MACD(12, 26, 9)
    p4_top, p4_bot = 928, 1018
    draw.rounded_rectangle((60, 904, 1540, 1046), radius=10, fill="#101c30")
    macd_vals = [
        float(v) for s in (macd_info["series"]["macd"], macd_info["series"]["signal"], macd_info["series"]["histogram"])
        for v in s if v is not None
    ]
    m_abs = max((abs(v) for v in macd_vals), default=1.0)
    if m_abs < 1e-6:
        m_abs = 1.0
    m_abs *= 1.15
    py4 = lambda val: p4_bot - ((float(val) + m_abs) / (2.0 * m_abs)) * (p4_bot - p4_top)
    zero_y = py4(0.0)
    draw.line((left, zero_y, right, zero_y), fill=grid, width=1)
    for i, hv in enumerate(macd_info["series"]["histogram"]):
        if hv is None:
            continue
        x = px(i)
        hy = py4(hv)
        col = "#4fd18b" if hv >= 0 else "#f87171"
        draw.rectangle((x - width / 2, min(zero_y, hy), x + width / 2, max(zero_y, hy)), fill=col)
    _draw_series_line(draw, macd_info["series"]["macd"], px, py4, macd_c, width=2)
    _draw_series_line(draw, macd_info["series"]["signal"], px, py4, sig_c, width=2)
    m_txt = f"{macd_info['macd']:+.1f}" if macd_info["macd"] is not None else "N/A"
    s_txt = f"{macd_info['signal']:+.1f}" if macd_info["signal"] is not None else "N/A"
    draw.text((84, 908), f"MACD(12,26,9): {m_txt}  /  Signal: {s_txt}  ({macd_info['state']})", font=font(14), fill=macd_c)

    # Shared date ticks along bottom of Panel 4
    for i in sorted({0, len(bars) // 4, len(bars) // 2, len(bars) * 3 // 4, len(bars) - 1}):
        x = max(left, min(right - 85, px(i) - 32))
        draw.text((x, 1022), bars[i].trade_date.isoformat()[5:], font=font(14), fill=muted)

    # Footer
    sup_txt = f"{sr_info['nearest_support']:,.0f}({sr_info['nearest_support_pct']:+.1f}%)" if sr_info["nearest_support"] else "-"
    res_txt = f"{sr_info['nearest_resistance']:,.0f}({sr_info['nearest_resistance_pct']:+.1f}%)" if sr_info["nearest_resistance"] else "-"
    draw.text((64, 1054), f"ATR14 {Decimal(m['atr14']):,.0f} ({Decimal(m['atr14'])/Decimal(m['close'])*100:.1f}%)   |   "
              + words("지지 ", "S ") + sup_txt + "  /  " + words("저항 ", "R ") + res_txt
              + f"   |   {words('캔들', 'CANDLE')}: {candle_info['pattern']}   |   {words('일봉', 'BARS')} {len(bars)}",
              font=font(16), fill=muted)
    draw.text((64, 1082), data["report_id"] + "   |   " + _label(", ".join(data["sources"]))
              + "   |   " + words("저장 자료 기반 기술 분석 · 자동 주문 없음", "SAVED DATA TECHNICAL ANALYSIS / NO AUTO ORDERS"),
              font=font(15), fill=ma20_c)

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ChartUnavailable("UNSAFE_OUTPUT_PATH")
    fd, temporary = tempfile.mkstemp(prefix=".chart-", suffix=".png", dir=target.parent)
    try:
        os.close(fd)
        image.save(temporary, format="PNG")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        image.close()
        if os.path.exists(temporary):
            os.unlink(temporary)


def candidate_charts(report, repository, output_dir: Path, *, renderer=render_chart):
    if report.get("contract") != "UNIFIED_RESEARCH_V1" or not REPORT_ID.fullmatch(report.get("report_id", "")):
        raise ValueError("INVALID_UNIFIED_REPORT")
    validate_analysis_time(datetime.fromisoformat(report["as_of"]))
    rows = report.get("candidates")
    if not isinstance(rows, list) or len(rows) > 20 or not all(isinstance(r, dict) for r in rows):
        raise ValueError("INVALID_CANDIDATE_LIST")
    if len({r.get("symbol") for r in rows}) != len(rows):
        raise ValueError("DUPLICATE_CANDIDATE_SYMBOL")
    output_dir = output_dir.resolve()
    if any(ord(c) < 32 for c in str(output_dir)):
        raise ValueError("INVALID_OUTPUT_PATH")
    items = []
    for row in rows:
        if row.get("action_decision") not in FINAL_DECISIONS:
            continue
        item = {"symbol": row.get("symbol"), "name": _label(row.get("name", "")),
                "decision": row["action_decision"]}
        try:
            data = prepare_chart(report, row, repository)
            directory = output_dir / report["report_id"]
            if directory.is_symlink():
                raise ChartUnavailable("UNSAFE_OUTPUT_PATH")
            key = hashlib.sha256((VERSION + RENDER_VERSION + data["price_input_fingerprint"]).encode()).hexdigest()[:16]
            target = directory / f"{data['symbol']}-{key}.png"
            renderer(data, target)
            item.update(status="READY", path=str(target), session=data["session"].isoformat(),
                        price_input_fingerprint=data["price_input_fingerprint"], metrics=data["metrics"],
                        technical_analysis=data["technical_analysis"],
                        chart_summary=summarize_for_committee(data["extended_analysis"]))
        except ChartUnavailable as exc:
            item.update(status="UNAVAILABLE", reason=str(exc))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            item.update(status="UNAVAILABLE", reason="CHART_DATA_OR_RENDER_FAILED")
        items.append(item)
    return {"contract": VERSION, "report_id": report["report_id"], "as_of": report["as_of"],
            "source_read_only": True, "orders_enabled": False, "report_modified": False,
            "compact": report.get("policy_mode") == "ACTIONABLE_COMMITTEE",
            "charts": items}


def _compact_chart_caption(item: dict) -> list[str]:
    analysis = item.get("technical_analysis") or {}
    methods = {m.get("code"): m.get("values", {}) for m in analysis.get("methods", [])}
    ohlc = methods.get("OHLC_CANDLE") or {}
    sma = methods.get("SMA20_60") or {}
    vol = methods.get("VOLUME_RATIO20") or {}
    atr = methods.get("WILDER_ATR14") or {}
    pattern = (analysis.get("stored_pattern") or {}).get("name")
    pattern_tag = f" · {pattern}" if pattern else ""
    header = f"📈 {item['name']} ({item['symbol']}){pattern_tag} ({item['session']})"

    parts = []
    if ohlc.get("close") is not None:
        candle = ohlc.get("candle") or ""
        candle_tag = f"({candle})" if candle else ""
        parts.append(f"종가 {Decimal(str(ohlc['close'])):,.0f}원{candle_tag}")
    pos_kr = {"ABOVE": "위", "BELOW": "아래", "EQUAL": "동일"}
    if sma.get("sma20") is not None and sma.get("sma60") is not None:
        p20 = pos_kr.get(sma.get("close_vs_sma20"), "")
        p60 = pos_kr.get(sma.get("close_vs_sma60"), "")
        parts.append(f"20일선 {p20}({Decimal(str(sma['sma20'])):,.0f}) / 60일선 {p60}({Decimal(str(sma['sma60'])):,.0f})")
    if vol.get("multiple") is not None:
        parts.append(f"거래량 {Decimal(str(vol['multiple'])):.2f}배")
    elif vol:
        parts.append("거래량 배율 확인 불가")
    if atr.get("atr_pct_of_close") is not None:
        parts.append(f"ATR {Decimal(str(atr['atr_pct_of_close'])):.1f}%")

    lines = [header, " · ".join(parts)]
    summary = item.get("chart_summary")
    if isinstance(summary, dict):
        rsi = summary.get("rsi14") or {}
        macd = summary.get("macd_12_26_9") or {}
        bb = summary.get("bollinger_20_2") or {}
        ichi = summary.get("ichimoku") or {}
        sr = summary.get("support_resistance") or {}
        vp = summary.get("volume_profile") or {}
        rsi_kr = {"OVERBOUGHT": "과매수", "BULLISH": "강세", "NEUTRAL": "중립", "WEAK": "약세", "OVERSOLD": "과매도"}
        macd_kr = {"BULLISH_CROSS": "골든크로스", "BEARISH_CROSS": "데드크로스",
                   "ABOVE_SIGNAL_EXPANDING": "상승확대", "ABOVE_SIGNAL_CONTRACTING": "상승둔화",
                   "BELOW_SIGNAL_IMPROVING": "반등시도", "BELOW_SIGNAL_WEAKENING": "하락우위"}
        cloud_kr = {"ABOVE_CLOUD": "구름 위", "IN_CLOUD": "구름 안", "BELOW_CLOUD": "구름 아래"}
        ext_parts = []
        if rsi.get("value") is not None:
            div_txt = "↑다이버전스" if rsi.get("divergence") == "BULLISH_DIVERGENCE" else "↓다이버전스" if rsi.get("divergence") == "BEARISH_DIVERGENCE" else ""
            ext_parts.append(f"RSI {rsi['value']:.1f}({rsi_kr.get(rsi.get('zone'), '-')}{div_txt})")
        if macd.get("state") in macd_kr:
            ext_parts.append(f"MACD {macd_kr[macd['state']]}")
        if bb.get("percent_b") is not None:
            sq = "/스퀴즈" if bb.get("squeeze") else ""
            ext_parts.append(f"볼린저 %B {bb['percent_b']:.2f}{sq}")
        if ichi.get("cloud_position") in cloud_kr:
            ext_parts.append(cloud_kr[ichi["cloud_position"]])
        if sr.get("nearest_support") and sr.get("nearest_resistance"):
            ext_parts.append(f"지지 {sr['nearest_support']:,.0f} / 저항 {sr['nearest_resistance']:,.0f}")
        if vp.get("poc_price") is not None:
            ov = f"(상단매물 {vp['overhead_supply_pct']:.0f}%)" if vp.get("overhead_supply_pct") is not None else ""
            ext_parts.append(f"매물대 POC {vp['poc_price']:,.0f}{ov}")
        if ext_parts:
            lines.append(" · ".join(ext_parts))
    lines.append("MEDIA:" + item["path"])
    return lines


def telegram_attachments(result, *, compact: bool | None = None):
    use_compact = bool(result.get("compact")) if compact is None else compact
    lines = []
    for item in result["charts"]:
        title = f"기술적 차트 · {item['name']} ({item['symbol']})"
        if item["status"] == "READY":
            if use_compact and item.get("technical_analysis"):
                if lines:
                    lines.append("")
                lines.extend(_compact_chart_caption(item))
            else:
                lines.extend([f"{title} · 일봉 {item['session']} · {LABELS[item['decision']]}",
                              explain_technical_analysis(item["technical_analysis"]) if item.get("technical_analysis")
                              else "지표 설명 정보 확인 필요 · 저장 보고서와 판정은 유지합니다.",
                              "MEDIA:" + item["path"]])
        else:
            reasons = {"PRICE_NOT_VERIFIED": "가격 확인 대기", "PRICE_HISTORY_UNAVAILABLE": "일봉 이력 부족",
                       "PRICE_SESSION_INCOMPLETE": "기준일 일봉 부족", "REPORT_PRICE_INPUT_MISMATCH": "보고서와 가격 자료 불일치",
                       "REPORT_TECHNICAL_METRIC_MISMATCH": "보고서와 지표 불일치", "PNG_LIBRARY_UNAVAILABLE": "이미지 생성 기능 사용 불가"}
            reason = reasons.get(item["reason"], "가격 자료 또는 차트 생성 확인 필요")
            lines.append(f"{title} 첨부 불가: {reason} · 저장 판정 유지")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--research-database", type=Path, required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "telegram"), default="json")
    args = parser.parse_args(argv)
    try:
        repo = ReadOnlyResearchRepository(args.database)
        report = load_report(args.research_database, args.report_id, source_database=args.database)
        result = candidate_charts(report, repo, args.output_dir)
        output = telegram_attachments(result) if args.format == "telegram" else json.dumps(result, ensure_ascii=False)
        if output:
            print(output)
        return 0
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError):
        print("CANDIDATE_CHARTS status=UNAVAILABLE reason=REPORT_OR_SOURCE_UNAVAILABLE", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
