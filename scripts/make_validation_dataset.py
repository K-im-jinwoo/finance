"""Synthetic statements are never a market performance claim."""
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from bootstrap import ROOT
from stock_assistant.models import AssetType, CompanyKind, Market, Security, FinancialSnapshot, OHLCV, Catalyst, CatalystStatus, to_json_value
from stock_assistant.screening import screen_security, select_top_candidates

KST = timezone(timedelta(hours=9))
SYMBOLS = ["900001", "900002", "900003", "900004", "900005", "900006"]

def make():
    # Synthetic weekdays, explicitly not a KRX holiday calendar.
    start = date(2026, 1, 5)
    days = []
    while len(days) < 70:
        if start.weekday() < 5:
            days.append(start)
        start += timedelta(days=1)
    results = []
    histories = {}
    at = datetime(2026, 4, 13, 8, 30, tzinfo=KST)
    for symbol in SYMBOLS:
        bars = []
        for index, day in enumerate(days):
            close = Decimal(50000 + index * 500)
            bars.append(OHLCV(symbol, day, close-100, close+200, close-200, close, 300000 if index == 69 else 100000, "SYNTHETIC", datetime.combine(day, time(16), KST)))
        histories[symbol] = [str(b.close) for b in bars]
        fin = FinancialSnapshot(symbol, date(2025,12,31), datetime(2026,3,20,16,tzinfo=KST), Decimal("1000000"), Decimal("2000000"), Decimal("500000"), (Decimal("1"),Decimal("1.1")), (Decimal("1"),Decimal("1.1")), "https://example.invalid/synthetic", Decimal("1000000"), Decimal("1000000"), date(2025,12,31), "https://example.invalid/synthetic")
        catalysts = (Catalyst(symbol, "검증 재료", CatalystStatus.PARTIAL, datetime(2026,4,10,16,tzinfo=KST), None, ()),) if symbol == SYMBOLS[0] else ()
        results.append(screen_security(Security(symbol, f"검증종목 {symbol[-1]}", Market.KOSPI, AssetType.COMMON, CompanyKind.GENERAL, date(2020,1,1)), bars, as_of=at, financial=fin, catalysts=catalysts))
    selected = to_json_value(select_top_candidates(results))
    events = []
    def add(kind, at, **kw):
        event = {"id": f"V-{len(events):03d}", "type": kind, "at": at, **kw}
        if kind in {"REPORT","CHECK","OPEN","MARK"}:
            event.update(observed_at=at, source="SYNTHETIC")
        events.append(event)
    def report(at, candidates=selected):
        add("REPORT", at, report_id="VALIDATION-REPORT-" + at, input_available_at=at, candidates=candidates)
    def opened(day, price):
        at = day + "T09:00:00+09:00"
        add("OPEN", at, open_at=at, calendar_available_at=day+"T08:00:00+09:00", calendar_source="SYNTHETIC_WEEKDAY_FIXTURE", session_id=day, prices={s: str(price) for s in SYMBOLS})
    def check(at, symbol, below=False, fresh=True, loss=False):
        price = {"id": f"P-{symbol}-{at}", "published_at": at, "observed_at": at, "available_at": at, "source": "SYNTHETIC", "fresh": fresh, "basis": "UNADJUSTED_CONSISTENT_NO_ACTIONS", "closes": ["84500"]*19 + ["80000" if below else "85000"]}
        fin = {"id": f"F-{symbol}-{at}", "published_at": at, "observed_at": at, "available_at": at, "source": "SYNTHETIC", "fresh": True, "periods_verified": True, "operating_income": "1", "annual_operating_income": "1", "ttm_operating_income": "-1" if loss else "1", "ocf": "1"}
        add("CHECK", at, symbol=symbol, price=price, financial=fin)
    def mark(at, value):
        add("MARK", at, prices={s: str(value) for s in SYMBOLS}, evening_summary=True, verified_trade_date=at[:10])
    report("2026-04-13T08:30:00+09:00")
    opened("2026-04-13", 84500)
    # Repetition has one holding interval and no extra entry.
    report("2026-04-13T20:00:00+09:00")
    for symbol in SYMBOLS[:5]:
        check("2026-04-13T20:01:00+09:00", symbol)
    mark("2026-04-13T20:02:00+09:00", 86000)
    opened("2026-04-14", 86000)
    # Candidate disappearance never triggers liquidation.
    report("2026-04-14T20:00:00+09:00", selected[1:])
    for symbol in SYMBOLS[:5]:
        check("2026-04-14T20:01:00+09:00", symbol, below=(symbol == SYMBOLS[0]))
    mark("2026-04-14T20:02:00+09:00", 80000)
    opened("2026-04-15", 81000)
    # Same invalidated data blocks reentry until a fresh valid observation.
    report("2026-04-15T20:00:00+09:00")
    check("2026-04-16T08:00:00+09:00", SYMBOLS[0])
    report("2026-04-16T08:30:00+09:00")
    opened("2026-04-16", 85000)
    for symbol in SYMBOLS[:5]:
        check("2026-04-16T20:01:00+09:00", symbol)
    mark("2026-04-16T20:02:00+09:00", 87000)
    add("END", "2026-04-16T20:03:00+09:00")
    return {"schema_version": 1, "mode": "SYNTHETIC", "securities": {s: {"name":f"검증종목 {s[-1]}","kind":"GENERAL"} for s in SYMBOLS},
            "lineage": {"selection": "vendored screen_security + select_top_candidates; no ranking changes", "source_head": "f3af37a66765e5fb0e500baf4e845c25210a67c9", "snapshot_manifest": "artifacts/source-audit.json", "synthetic_warmup_bars": histories},
            "limitations": ["All prices/statements/signals/calendar are synthetic", "No three-year market coverage", "No benchmark or corporate actions"], "events": events}

if __name__ == "__main__":
    data = make()
    (ROOT / "datasets/validation.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"Created SYNTHETIC dataset with {len(data['events'])} events")

