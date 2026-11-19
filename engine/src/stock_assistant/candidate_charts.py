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
RENDER_VERSION = "2026-11-19.2"
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


def _place_non_overlapping_y(raw_y: float, used: list[float], lo_y: float, hi_y: float, min_gap: float = 20.0) -> float:
    y = max(lo_y, min(hi_y, raw_y))
    for _ in range(8):
        if all(abs(y - prev) >= min_gap for prev in used):
            break
        y = min(hi_y, y + min_gap) if y + min_gap <= hi_y else max(lo_y, y - min_gap)
    used.append(y)
    return y


def render_chart(data, target: Path, *, font_file=None, demo=False):
    """Draw analyst-annotated chart (~70% main price/patterns/trendlines/zones + compact volume & RSI|MACD)."""
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
    bb_c, sup_c, res_c, stop_c, poc_c = "#4e6a94", "#4fd18b", "#f87171", "#fb923c", "#f6c453"
    pat_c, tri_c, rsi_c, macd_c, sig_c = "#38bdf8", "#facc15", "#90cdf4", "#63b3ed", "#f6ad55"
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
    trendlines = ext.get("trendlines") or {}
    patterns = ext.get("patterns") or {}
    primary_pat = patterns.get("primary")
    trade_plan = ext.get("trade_plan") or {}
    grade = trade_plan.get("grade", "ENTRY_READY")

    # Header with execution grade badge & detected pattern badge
    title = f"{data['name']}  {data['symbol']}" if korean else data["symbol"]
    draw.text((56, 18), title, font=font(32), fill=ink)

    grade_styles = {
        "ENTRY_READY": ("#123c2b", "#4fd18b", words("진입 검토", "ENTRY READY")),
        "WAIT_BREAKOUT": ("#3b2f12", "#f6c453", words("돌파 대기", "WAIT BREAKOUT")),
        "WAIT_PULLBACK": ("#422414", "#fb923c", words("눌림 대기(과열)", "WAIT PULLBACK")),
        "CHART_CAUTION": ("#42181f", "#f87171", words("차트 주의", "CAUTION")),
    }
    g_bg, g_fg, g_txt = grade_styles.get(grade, ("#14213a", ma20_c, grade))
    bx = 56
    by = 62
    badge_w = max(140, len(g_txt) * (16 if korean else 11) + 28)
    draw.rounded_rectangle((bx, by, bx + badge_w, by + 30), radius=8, fill=g_bg, outline=g_fg, width=1)
    draw.text((bx + 14, by + 5), g_txt, font=font(17), fill=g_fg)
    bx += badge_w + 12

    if primary_pat:
        st_ko = "돌파" if primary_pat.get("status") == "BREAKOUT" else "이탈" if primary_pat.get("status") == "BREAKDOWN" else "형성"
        p_txt = words(f"패턴: {primary_pat['name_ko']} {st_ko}", f"PATTERN: {primary_pat['code']} {primary_pat['status']}")
        pw = max(160, len(p_txt) * (13 if korean else 9) + 24)
        p_col = res_c if primary_pat.get("bias") == "BEARISH" else pat_c
        draw.rounded_rectangle((bx, by, bx + pw, by + 30), radius=8, fill="#112942", outline=p_col, width=1)
        draw.text((bx + 12, by + 5), p_txt, font=font(16), fill=p_col)
        bx += pw + 12

    reasons_txt = " · ".join(trade_plan.get("reasons") or []) if korean else ""
    meta_line = (
        words("일봉", "CLOSE") + f" {data['session']}  |  "
        + data["as_of"].astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
        + (f"  |  {reasons_txt}" if reasons_txt else "")
    )
    draw.text((bx + 4, by + 6), meta_line[:90], font=font(16), fill=muted)
    if demo:
        draw.text((920, 22), words("표시 예시 · 가상 데이터 · 추천 아님", "DEMO / SYNTHETIC / NOT A RECOMMENDATION"),
                  font=font(20), fill=up)

    # 4 compact summary cards (104..174)
    m = data["metrics"]
    align_kr = {"BULLISH_FULL": "완전 정배열", "BULLISH_MID": "중기 정배열",
                "BEARISH_FULL": "완전 역배열", "BEARISH_MID": "중기 역배열", "MIXED": "혼조"}
    brk_p = trade_plan.get("breakout_price")
    brk_pct = trade_plan.get("breakout_pct")
    sup_p = trade_plan.get("support_price")
    stop_p = trade_plan.get("stop_loss")
    stop_pct = trade_plan.get("stop_loss_pct")
    poc_str = f"{vp_info['poc_price']:,.0f}" if vp_info.get("poc_price") is not None else "N/A"
    ov_str = f"{vp_info['overhead_supply_pct']:.0f}%" if vp_info.get("overhead_supply_pct") is not None else "N/A"
    cards = [
        (words("종가 · 이평배열", "CLOSE / MA ALIGN"),
         f"{Decimal(m['close']):,.0f}  {words(align_kr.get(ma_info['alignment'], '혼조'), ma_info['alignment'])}", ink),
        (words("돌파선 (저항)", "BREAKOUT / RES"),
         f"{brk_p:,.0f} ({brk_pct:+.1f}%)" if brk_p is not None and brk_pct is not None else words("확인 불가", "N/A"), res_c),
        (words("지지선 / 손절(무효화)", "SUPPORT / STOP"),
         f"{sup_p:,.0f} / {stop_p:,.0f} ({stop_pct:+.1f}%)" if sup_p is not None and stop_p is not None and stop_pct is not None else words("확인 불가", "N/A"), sup_c),
        (words("매물대 POC · 상단매물", "POC / OVERHEAD"),
         f"{poc_str}  |  {words('상단 ', 'UP ')}{ov_str}", poc_c),
    ]
    for i, (label, value, vcol) in enumerate(cards):
        x = 56 + i * 376
        draw.rounded_rectangle((x, 106, x + 362, 178), radius=10, fill="#14213a")
        draw.text((x + 14, 114), label, font=font(14), fill=muted)
        draw.text((x + 14, 140), value, font=font(20), fill=vcol)

    # Panel 1 (~70% chart area): Main price + zones + trendlines + 6 classic patterns + S/R/Stop + Volume Profile
    left, right = 82, 1306
    vp_left, vp_right = 1318, 1456
    p1_box_top, p1_box_bot = 188, 760
    p1_top, p1_bot = 224, 742
    draw.rounded_rectangle((56, p1_box_top, 1544, p1_box_bot), radius=12, fill="#101c30")

    bars = data["bars"]
    px = lambda i: left + (i + 0.5) * (right - left) / len(bars)
    width = max(2, int((right - left) / len(bars) * 0.60))

    price_vals = [float(b.low) for b in bars] + [float(b.high) for b in bars]
    for s in (bb_info["series"]["upper"], bb_info["series"]["lower"]):
        price_vals.extend(float(v) for v in s if v is not None)
    for extra in (brk_p, sup_p, stop_p):
        if extra is not None:
            price_vals.append(float(extra))
    lo, hi = min(price_vals), max(price_vals)
    padding = max((hi - lo) * 0.07, hi * 0.018, 1.0)
    lo, hi = lo - padding, hi + padding
    py1 = lambda val: p1_bot - (float(val) - lo) / (hi - lo) * (p1_bot - p1_top)

    # Shaded resistance / support zones (subtle background bands)
    if brk_p is not None and lo < float(brk_p) < hi:
        y_brk = py1(float(brk_p))
        draw.rectangle((left, max(p1_top, y_brk - 18), right, y_brk), fill="#211626")
    if sup_p is not None and lo < float(sup_p) < hi:
        y_sup = py1(float(sup_p))
        draw.rectangle((left, y_sup, right, min(p1_bot, y_sup + 18)), fill="#102926")

    # Box Range shaded channel if detected
    if primary_pat and primary_pat.get("code") == "BOX_RANGE":
        bx1, bx2 = px(primary_pat["start_idx"]), px(primary_pat["end_idx"])
        by_top, by_bot = py1(float(primary_pat["top"])), py1(float(primary_pat["bottom"]))
        draw.rectangle((bx1, by_top, bx2, by_bot), fill="#172942", outline=pat_c, width=2)

    # Horizontal grid + right price axis
    for i in range(6):
        val = lo + (hi - lo) * i / 5
        y = py1(val)
        draw.line((left, y, right, y), fill=grid, width=1)
        draw.text((vp_right + 10, y - 8), f"{val:,.0f}", font=font(14), fill=muted)

    # Volume Profile bars + POC line
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
        if vp_info.get("poc_price") is not None and lo <= float(vp_info["poc_price"]) <= hi:
            poc_y = py1(float(vp_info["poc_price"]))
            _draw_dashed_hline(draw, left, vp_right, poc_y, poc_c, dash=5, gap=6, width=1)
            draw.text((vp_left + 2, max(p1_top, poc_y - 16)), "POC", font=font(13), fill=poc_c)

    # Bollinger Bands (subtle)
    _draw_series_line(draw, bb_info["series"]["upper"], px, py1, bb_c, width=1)
    _draw_series_line(draw, bb_info["series"]["lower"], px, py1, bb_c, width=1)

    # Candlesticks
    for i, b in enumerate(bars):
        x = px(i)
        color = up if b.close > b.open else down if b.close < b.open else muted
        draw.line((x, py1(b.high), x, py1(b.low)), fill=color, width=2)
        y1, y2 = sorted((py1(b.open), py1(b.close)))
        draw.rectangle((x - width / 2, y1, x + width / 2, max(y1 + 2, y2)), fill=color)

    # Multi moving averages (5 / 20 / 60 / 120)
    for period, color, lw in ((5, ma5_c, 1), (20, ma20_c, 2), (60, ma60_c, 2), (120, ma120_c, 2)):
        _draw_series_line(draw, ma_info["series"][period], px, py1, color, width=lw)

    # Diagonal Trendlines (Support / Resistance)
    sup_line = trendlines.get("support_line")
    if sup_line and lo <= float(sup_line["y1"]) <= hi and lo <= float(sup_line["y2"]) <= hi:
        draw.line(
            (px(sup_line["x1"]), py1(sup_line["y1"]), px(sup_line["x2"]), py1(sup_line["y2"])),
            fill=sup_c, width=3,
        )
    res_line = trendlines.get("resistance_line")
    if res_line and lo <= float(res_line["y1"]) <= hi and lo <= float(res_line["y2"]) <= hi:
        draw.line(
            (px(res_line["x1"]), py1(res_line["y1"]), px(res_line["x2"]), py1(res_line["y2"])),
            fill=stop_c, width=3,
        )

    # Direct Geometric Pattern Drawings (6 classic patterns)
    if primary_pat:
        pcode = primary_pat.get("code")
        if pcode in {"DOUBLE_BOTTOM", "DOUBLE_TOP", "INVERSE_HEAD_AND_SHOULDERS"}:
            pts = [(px(idx), py1(float(val))) for idx, val in primary_pat.get("points", [])]
            pcol = res_c if pcode == "DOUBLE_TOP" else pat_c
            if len(pts) >= 2:
                draw.line(pts, fill=pcol, width=3)
            for cx, cy in pts:
                draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=tri_c, outline=pcol, width=2)
            nk = primary_pat.get("neckline")
            if nk is not None and lo <= float(nk) <= hi:
                ny = py1(float(nk))
                draw.line((pts[0][0], ny, right, ny), fill=tri_c, width=2)
                draw.rectangle((pts[0][0] + 4, max(p1_top, ny - 19), pts[0][0] + 150, max(p1_top, ny - 19) + 17), fill="#101c30")
                draw.text((pts[0][0] + 7, max(p1_top, ny - 18)),
                          words(f"{primary_pat['name_ko']} 넥라인", f"{pcode} NECK"), font=font(13), fill=tri_c)
        elif pcode == "CUP_AND_HANDLE":
            c_start = primary_pat["cup_start_idx"]
            c_low = primary_pat["cup_low_idx"]
            h_start = primary_pat["handle_start_idx"]
            c_end = primary_pat["end_idx"]
            rim_y = py1(float(primary_pat["rim_price"]))
            cup_pts = [
                (px(c_start), rim_y),
                (px((c_start + c_low) // 2), py1((float(primary_pat["rim_price"]) + float(primary_pat["cup_low"])) / 2.0)),
                (px(c_low), py1(float(primary_pat["cup_low"]))),
                (px((c_low + h_start) // 2), py1((float(primary_pat["rim_price"]) + float(primary_pat["cup_low"])) / 2.0)),
                (px(h_start), rim_y),
            ]
            draw.line(cup_pts, fill=pat_c, width=3)
            draw.rectangle((px(h_start), rim_y, px(c_end), py1(float(primary_pat["handle_low"]))), outline=tri_c, width=2)
            draw.line((px(c_start), rim_y, right, rim_y), fill=tri_c, width=2)
        elif pcode == "TRIANGLE":
            u = primary_pat["upper_line"]
            l = primary_pat["lower_line"]
            draw.line((px(u[0]), py1(float(u[1])), px(u[2]), py1(float(u[3]))), fill=tri_c, width=3)
            draw.line((px(l[0]), py1(float(l[1])), px(l[2]), py1(float(l[3]))), fill=sup_c, width=3)

    # Clear Horizontal Price Lines (Breakout / Support / Stop-Loss) with non-overlapping labels
    used_label_y: list[float] = []
    key_lines = []
    if brk_p is not None and lo <= float(brk_p) <= hi:
        key_lines.append((float(brk_p), res_c, words(f"돌파선 {brk_p:,.0f} ({brk_pct:+.1f}%)", f"BRK {brk_p:,.0f} ({brk_pct:+.1f}%)")))
    if stop_p is not None and lo <= float(stop_p) <= hi:
        key_lines.append((float(stop_p), stop_c, words(f"손절(20일선) {stop_p:,.0f} ({stop_pct:+.1f}%)", f"STOP {stop_p:,.0f} ({stop_pct:+.1f}%)")))
    if sup_p is not None and (stop_p is None or abs(float(sup_p) - float(stop_p)) / max(float(sup_p), 1.0) > 0.01) and lo <= float(sup_p) <= hi:
        sup_pct = trade_plan.get("support_pct") or 0.0
        key_lines.append((float(sup_p), sup_c, words(f"지지선 {sup_p:,.0f} ({sup_pct:+.1f}%)", f"SUP {sup_p:,.0f} ({sup_pct:+.1f}%)")))
    for level_val, lcol, ltxt in key_lines:
        y = py1(level_val)
        _draw_dashed_hline(draw, left, right, y, lcol, dash=9, gap=6, width=2)
        ly = _place_non_overlapping_y(y - 18, used_label_y, p1_top + 4, p1_bot - 22, min_gap=22.0)
        lw_box = max(145, len(ltxt) * (10 if korean else 8) + 14)
        draw.rectangle((left + 4, ly, left + 4 + lw_box, ly + 19), fill="#0d182b", outline=lcol, width=1)
        draw.text((left + 9, ly + 2), ltxt, font=font(13), fill=lcol)

    # Panel 1 Legend (top-left) + Key Annotation Chips (top-right inside main chart)
    draw.text((80, 194), "MA5", font=font(14), fill=ma5_c)
    draw.text((126, 194), "MA20", font=font(14), fill=ma20_c)
    draw.text((184, 194), "MA60", font=font(14), fill=ma60_c)
    draw.text((242, 194), "MA120", font=font(14), fill=ma120_c)
    draw.text((310, 194), "BB", font=font(14), fill=bb_c)
    draw.text((346, 194), words("추세·지지", "TREND/SUP"), font=font(14), fill=sup_c)
    draw.text((436, 194), words("돌파·저항", "BRK/RES"), font=font(14), fill=res_c)
    draw.text((524, 194), words("매물대(POC)", "VP/POC"), font=font(14), fill=poc_c)

    if korean and trade_plan.get("annotations"):
        ax = right - 4
        for note in reversed(trade_plan["annotations"][:3]):
            nw = max(90, len(note) * 11 + 18)
            ax -= nw
            if ax < 640:
                break
            n_col = res_c if ("과열" in note or "매물벽" in note or "약세" in note or "이탈" in note) else pat_c
            draw.rounded_rectangle((ax, 193, ax + nw, 215), radius=6, fill="#16243d", outline=n_col, width=1)
            draw.text((ax + 8, 196), note, font=font(13), fill=n_col)
            ax -= 8

    # Compact Panel 2 (Volume + OBV): 768..878
    p2_top, p2_bot = 796, 868
    draw.rounded_rectangle((56, 768, 1544, 878), radius=10, fill="#101c30")
    max_volume = max((b.volume for b in bars), default=0)
    for i, b in enumerate(bars):
        x = px(i)
        color = up if b.close > b.open else down if b.close < b.open else muted
        h = (b.volume / max(1, max_volume)) * (p2_bot - p2_top)
        draw.rectangle((x - width / 2, p2_bot - h, x + width / 2, p2_bot), fill=color)
    obv_kr = {"ACCUMULATION": "매집(상승)", "DISTRIBUTION": "분산(하락)", "FLAT": "중립", "UNAVAILABLE": "-"}
    draw.text((80, 773), words("거래량", "VOLUME") + f" max {max_volume:,}   |   OBV(20d): "
              + words(obv_kr.get(obv_info["trend_20d"], "-"), obv_info["trend_20d"]), font=font(14), fill=muted)

    # Compact Panel 3 (Split Left: RSI14 | Right: MACD 12/26/9): 886..1054
    p3_box_top, p3_box_bot = 886, 1054
    p3_top, p3_bot = 918, 1024
    # Left half: RSI(14)
    rsi_left, rsi_right = 82, 738
    draw.rounded_rectangle((56, p3_box_top, 790, p3_box_bot), radius=10, fill="#101c30")
    px_rsi = lambda i: rsi_left + (i + 0.5) * (rsi_right - rsi_left) / len(bars)
    py_rsi = lambda val: p3_bot - (max(0.0, min(100.0, float(val))) / 100.0) * (p3_bot - p3_top)
    for lvl, col in ((70, res_c), (50, grid), (30, sup_c)):
        y = py_rsi(lvl)
        _draw_dashed_hline(draw, rsi_left, rsi_right, y, col, dash=5, gap=5, width=1)
        draw.text((rsi_right + 8, y - 7), str(lvl), font=font(13), fill=col if lvl != 50 else muted)
    _draw_series_line(draw, rsi_info["series"], px_rsi, py_rsi, rsi_c, width=2)
    rsi_val_str = f"{rsi_info['value']:.1f}" if rsi_info["value"] is not None else "N/A"
    div_tag = f"  [{rsi_info['divergence']}]" if rsi_info["divergence"] != "NONE" else ""
    draw.text((80, 892), f"RSI(14): {rsi_val_str} ({rsi_info['zone']}){div_tag}", font=font(14), fill=rsi_c)
    for i in (0, len(bars) // 2, len(bars) - 1):
        draw.text((max(rsi_left, min(rsi_right - 50, px_rsi(i) - 22)), 1030), bars[i].trade_date.isoformat()[5:], font=font(13), fill=muted)

    # Right half: MACD(12, 26, 9)
    macd_left, macd_right = 830, 1510
    draw.rounded_rectangle((806, p3_box_top, 1544, p3_box_bot), radius=10, fill="#101c30")
    px_macd = lambda i: macd_left + (i + 0.5) * (macd_right - macd_left) / len(bars)
    mw = max(2, int((macd_right - macd_left) / len(bars) * 0.55))
    macd_vals = [
        float(v) for s in (macd_info["series"]["macd"], macd_info["series"]["signal"], macd_info["series"]["histogram"])
        for v in s if v is not None
    ]
    m_abs = max((abs(v) for v in macd_vals), default=1.0)
    if m_abs < 1e-6:
        m_abs = 1.0
    m_abs *= 1.15
    py_macd = lambda val: p3_bot - ((float(val) + m_abs) / (2.0 * m_abs)) * (p3_bot - p3_top)
    zero_y = py_macd(0.0)
    draw.line((macd_left, zero_y, macd_right, zero_y), fill=grid, width=1)
    for i, hv in enumerate(macd_info["series"]["histogram"]):
        if hv is None:
            continue
        x = px_macd(i)
        hy = py_macd(hv)
        col = "#4fd18b" if hv >= 0 else "#f87171"
        draw.rectangle((x - mw / 2, min(zero_y, hy), x + mw / 2, max(zero_y, hy)), fill=col)
    _draw_series_line(draw, macd_info["series"]["macd"], px_macd, py_macd, macd_c, width=2)
    _draw_series_line(draw, macd_info["series"]["signal"], px_macd, py_macd, sig_c, width=2)
    m_txt = f"{macd_info['macd']:+.1f}" if macd_info["macd"] is not None else "N/A"
    s_txt = f"{macd_info['signal']:+.1f}" if macd_info["signal"] is not None else "N/A"
    macd_short = {
        "BULLISH_CROSS": "골든크로스", "BEARISH_CROSS": "데드크로스",
        "ABOVE_SIGNAL_EXPANDING": "상승확대", "ABOVE_SIGNAL_CONTRACTING": "상승둔화",
        "BELOW_SIGNAL_IMPROVING": "반등시도", "BELOW_SIGNAL_WEAKENING": "하락우위",
    }
    macd_short_en = {
        "BULLISH_CROSS": "BULL CROSS", "BEARISH_CROSS": "BEAR CROSS",
        "ABOVE_SIGNAL_EXPANDING": "EXPANDING", "ABOVE_SIGNAL_CONTRACTING": "SLOWING",
        "BELOW_SIGNAL_IMPROVING": "REBOUND", "BELOW_SIGNAL_WEAKENING": "WEAKENING",
    }
    draw.text((830, 892), f"MACD(12,26,9): {m_txt} / {s_txt} ({words(macd_short.get(macd_info['state'], '-'), macd_short_en.get(macd_info['state'], '-'))})",
              font=font(14), fill=macd_c)
    for i in (0, len(bars) // 2, len(bars) - 1):
        draw.text((max(macd_left, min(macd_right - 50, px_macd(i) - 22)), 1030), bars[i].trade_date.isoformat()[5:], font=font(13), fill=muted)

    # Footer
    cloud_kr = {"ABOVE_CLOUD": "구름 위", "IN_CLOUD": "구름 안", "BELOW_CLOUD": "구름 아래", "UNAVAILABLE": "-"}
    draw.text((60, 1064), f"ATR14 {Decimal(m['atr14']):,.0f} ({Decimal(m['atr14'])/Decimal(m['close'])*100:.1f}%)   |   "
              + words("일목: ", "CLOUD: ") + words(cloud_kr.get(ichi_info["cloud_position"], "-"), ichi_info["cloud_position"])
              + f"   |   {words('캔들', 'CANDLE')}: {candle_info['pattern']}   |   {words('일봉', 'BARS')} {len(bars)}   |   {data['report_id']}",
              font=font(15), fill=muted)
    draw.text((60, 1090), _label(", ".join(data["sources"]))
              + "   |   " + words("저장 자료 기반 자동 작도 분석 · 실제 매매 전 최신 시세 확인 필수 · 자동 주문 없음",
                                  "SAVED DATA AUTO-ANNOTATED ANALYSIS / NO AUTO ORDERS"),
              font=font(14), fill=ma20_c)

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
        tp = summary.get("trade_plan") or {}
        pat = summary.get("primary_pattern")
        if tp.get("grade_label"):
            tp_parts = [tp["grade_label"]]
            if isinstance(pat, dict) and pat.get("name_ko"):
                st_ko = "돌파" if pat.get("status") == "BREAKOUT" else "이탈" if pat.get("status") == "BREAKDOWN" else "형성"
                tp_parts.append(f"{pat['name_ko']}({st_ko})")
            if tp.get("breakout_price") is not None:
                tp_parts.append(f"돌파 {tp['breakout_price']:,.0f}")
            if tp.get("support_price") is not None:
                tp_parts.append(f"지지 {tp['support_price']:,.0f}")
            if tp.get("stop_loss") is not None:
                tp_parts.append(f"손절 {tp['stop_loss']:,.0f}")
            lines.append(" · ".join(tp_parts))
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
