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

from .indicators import price_features, simple_average
from .models import to_json_value
from .research_data import APPLICATION_ID
from .research_repository import ReadOnlyResearchRepository
from .validation import validate_analysis_time


VERSION = "CANDIDATE_CHARTS_V1"
RENDER_VERSION = "2026-10-20.2"
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


def render_chart(data, target: Path, *, font_file=None, demo=False):
    """Draw actual OHLC candles; every plotted value comes from prepare_chart."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ChartUnavailable("PNG_LIBRARY_UNAVAILABLE") from exc
    font_path, korean = _font_file(font_file)
    font = lambda size: ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default(size=size)
    image = Image.new("RGB", (1600, 1120), "#0b1324")
    draw = ImageDraw.Draw(image)
    ink, muted, grid = "#e9eff8", "#9cadc7", "#28364c"
    up, down, ma20, ma60 = "#ef6874", "#64aaff", "#ffcc72", "#68dbc8"
    words = (lambda ko, en: ko if korean else en)
    title = f"{data['name']}  {data['symbol']}" if korean else data["symbol"]
    draw.text((70, 35), title, font=font(38), fill=ink)
    status = LABELS[data["decision"]] if korean else ("WATCH" if data["decision"] == "WATCH" else "NEXT SESSION: CONDITIONAL")
    draw.text((70, 91), status, font=font(23), fill=ma20)
    if demo:
        draw.text((750, 48), words("표시 예시 · 가상 데이터 · 추천 아님", "DEMO / SYNTHETIC / NOT A RECOMMENDATION"),
                  font=font(24), fill=up)
    draw.text((70, 132), words("일봉 기준일", "DAILY CLOSE") + f" {data['session']}   |   "
              + words("보고서 시각", "REPORT AS OF") + " " + data["as_of"].astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
              font=font(20), fill=muted)
    m = data["metrics"]
    analysis = data.get("technical_analysis") or technical_analysis(data)
    volume_method = next(method for method in analysis["methods"] if method["code"] == "VOLUME_RATIO20")
    volume_multiple = volume_method["values"]["multiple"]
    cards = [(words("종가", "CLOSE"), f"{Decimal(m['close']):,.0f}"),
             (words("20일 이동평균", "SMA 20"), f"{Decimal(m['sma20']):,.2f}"),
             (words("60일 이동평균", "SMA 60"), f"{Decimal(m['sma60']):,.2f}"),
             (words("직전 20일 대비 거래량", "VOL / PRIOR 20 AVG"),
              f"{Decimal(volume_multiple):.2f}x" if volume_multiple is not None else words("확인 불가", "N/A"))]
    for i, (label, value) in enumerate(cards):
        x = 70 + i * 375
        draw.rounded_rectangle((x, 184, x + 345, 282), radius=12, fill="#14213a")
        draw.text((x + 18, 199), label, font=font(18), fill=muted)
        draw.text((x + 18, 230), value, font=font(28), fill=ink)
    left, right, top, bottom = 100, 1440, 336, 772
    draw.rounded_rectangle((70, 310, 1530, 995), radius=16, fill="#101c30")
    bars = data["bars"]
    lo, hi = float(min(b.low for b in bars)), float(max(b.high for b in bars))
    padding = max((hi - lo) * .08, hi * .015)
    lo, hi = lo - padding, hi + padding
    px = lambda i: left + (i + .5) * (right - left) / len(bars)
    py = lambda value: bottom - (float(value) - lo) / (hi - lo) * (bottom - top)
    for i in range(6):
        value = lo + (hi - lo) * i / 5
        y = py(value)
        draw.line((left, y, right, y), fill=grid)
        draw.text((right + 15, y - 10), f"{value:,.0f}", font=font(16), fill=muted)
    width = max(2, int((right - left) / len(bars) * .6))
    max_volume = max(b.volume for b in bars)
    for i, b in enumerate(bars):
        x = px(i)
        color = up if b.close > b.open else down if b.close < b.open else muted
        draw.line((x, py(b.high), x, py(b.low)), fill=color, width=2)
        y1, y2 = sorted((py(b.open), py(b.close)))
        draw.rectangle((x - width / 2, y1, x + width / 2, max(y1 + 2, y2)), fill=color)
        y = 947 - b.volume / max(1, max_volume) * 115
        draw.rectangle((x - width / 2, y, x + width / 2, 947), fill=color)
    for period, color in ((20, ma20), (60, ma60)):
        points = [(px(i), py(value)) for i, value in enumerate(data["averages"][period]) if value is not None]
        if len(points) > 1:
            draw.line(points, fill=color, width=3)
    draw.text((110, 316), "SMA 20", font=font(17), fill=ma20)
    draw.text((230, 316), "SMA 60", font=font(17), fill=ma60)
    draw.text((380, 316), words("사용 기법: 일봉 캔들 · 단순이동평균 · 거래량 비교 · Wilder ATR14",
                               "METHODS: OHLC / SMA20+60 / VOLUME RATIO / WILDER ATR14"), font=font(16), fill=muted)
    draw.text((110, 800), words("거래량 (주)", "VOLUME (SHARES)") + f"   max {max_volume:,}", font=font(17), fill=muted)
    for i in sorted({0, len(bars) // 4, len(bars) // 2, len(bars) * 3 // 4, len(bars) - 1}):
        x = max(left, min(right - 95, px(i) - 42))
        draw.text((x, 958), bars[i].trade_date.isoformat()[5:], font=font(17), fill=muted)
    draw.text((80, 1014), f"ATR14  {Decimal(m['atr14']):,.2f} KRW   |   "
              + words("일봉", "BARS") + f" {len(bars)}   |   " + words("출처", "SOURCE") + " "
              + _label(", ".join(data["sources"])), font=font(18), fill=muted)
    draw.text((80, 1050), data["report_id"] + "   |   " + words("저장 자료 참고 · 현재 진입 신호 아님", "SAVED DATA / NOT A CURRENT ENTRY SIGNAL"),
              font=font(18), fill=ma20)
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
                        technical_analysis=data["technical_analysis"])
        except ChartUnavailable as exc:
            item.update(status="UNAVAILABLE", reason=str(exc))
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
            item.update(status="UNAVAILABLE", reason="CHART_DATA_OR_RENDER_FAILED")
        items.append(item)
    return {"contract": VERSION, "report_id": report["report_id"], "as_of": report["as_of"],
            "source_read_only": True, "orders_enabled": False, "report_modified": False,
            "charts": items}


def telegram_attachments(result):
    lines = []
    for item in result["charts"]:
        title = f"기술적 차트 · {item['name']} ({item['symbol']})"
        if item["status"] == "READY":
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
