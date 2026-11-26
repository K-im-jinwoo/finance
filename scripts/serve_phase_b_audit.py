"""Serve disposable synthetic audit data on loopback. Builds no production state."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import threading

from bootstrap import ROOT
from daytrade_dashboard import ReadOnlyHandler
from research_dashboard.daytrade_bridge import simulate_sample_daytrade
from research_dashboard.rules import Policy
from research_dashboard.server import DashboardServer
from stock_assistant.backtest import SWING_POLICY
from stock_assistant.models import Decision, Holding, OHLCV, ScreeningResult, ThesisCard, ThesisStatus
from stock_assistant.news_cost import NewsApiBudget
from stock_assistant.performance import PerformanceRecord, PerformanceStatus
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData


UTC = timezone.utc
SESSION = date(2026, 11, 13)
AS_OF = datetime.combine(SESSION, time(9), UTC)
UR = "UR-" + "a" * 24
SW = "SW-" + "c" * 24
REGULAR = "R-20261113T0900Z-AB12CD34"
ISSUERS = (
    ("900101", "합성 반도체", "BUY_RECOMMENDATION"),
    ("900102", "합성 모빌리티", "WATCH"),
    ("900103", "합성 전력", "VERIFY_PENDING"),
    ("900104", "합성 소재", "EXCLUDED"),
    ("900105", "합성 유통", "NOT_APPLICABLE"),
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def weekdays(end, count):
    days = []
    while len(days) < count:
        if end.weekday() < 5:
            days.append(end)
        end -= timedelta(days=1)
    return list(reversed(days))


def article(index, category, title):
    return {"title": title, "category": category, "published_at": AS_OF.isoformat(),
            "url": f"https://example.invalid/phase-b-audit/{index}", "excerpt": "감사용 합성 기사 근거입니다."}


def seed_reports(store):
    for identifier, day in (("UR-" + "b" * 24, SESSION - timedelta(days=1)), (UR, SESSION)):
        as_of = datetime.combine(day, time(9), UTC)
        evaluations = []
        for index, (symbol, name, verdict) in enumerate(ISSUERS):
            evaluations.append({
                "symbol": symbol, "name": name, "action_decision": verdict,
                "financial_score": str(88 - index * 9), "news_score": str(74 - index * 11),
                "risk_penalty": str(index * 3), "total_score": str(Decimal("83.8") - index * Decimal("12.6")),
                "financial_breakdown": {
                    "profit": {"points": 15 - index, "maximum": 20, "status": "SCORED", "reason": "합성 영업이익 추세"},
                    "cash_flow": {"points": 13 - index, "maximum": 15, "status": "SCORED", "reason": "합성 영업현금흐름"},
                },
                "price": {
                    "session": day.isoformat(), "strategy": "MOMENTUM_CONTINUATION",
                    "entry_conditions": ["이전 합성 고점 돌파와 거래량 증가를 함께 확인합니다."],
                    "invalidation_conditions": ["합성 종가가 직전 저점보다 낮아지면 무효로 봅니다."],
                },
                "pending_checks": ["다음 합성 현금흐름 공시를 확인합니다."] if index else [],
                "advisories": ["감사용 합성 사례입니다."],
                "news_events": [{"event_id": f"AUDIT-{index}", "category": "CONTRACT", "status": "VERIFIED_ACTIVE",
                                 "points": 15 - index, "title": "합성 계약 보고"}],
                "recent_headlines": [article(index, "CONTRACT", "합성 계약 공시")],
            })
        store.put("unified_report", "ALL", {
            "report_id": identifier, "contract": "UNIFIED_RESEARCH_V1", "policy_version": "UNIFIED_RESEARCH_V1",
            "mode": "SYNTHETIC", "as_of": as_of.isoformat(), "expected_price_session": day.isoformat(),
            "candidates": evaluations[:1], "evaluations": evaluations,
            "decision_counts": {verdict: 1 for _, _, verdict in ISSUERS},
        }, observed_at=as_of)

    candidates = []
    for index, (category, title, duration) in enumerate((
        ("CONTRACT", "합성 장비 공급 계약", "POTENTIALLY_PERSISTENT"),
        ("EARNINGS", "합성 분기 실적 공개", "ONE_OFF"),
        ("POLICY", "합성 전력 정책 발표", "UNCLEAR"),
    )):
        symbol, name, _ = ISSUERS[index]
        candidates.append({
            "symbol": symbol, "name": name, "action_decision": "WATCH",
            "turnover": str(13_200_000_000 - index * 940_000_000), "turnover_multiple": str(Decimal("3.6") - index),
            "return_5d": str(Decimal("0.076") - index * Decimal("0.034")),
            "news_evidence": [article(index + 10, category, title)],
            "hermes_duration": duration, "hermes_evidence_index": 0,
        })
    store.publish_swing_report(SESSION, {
        "report_id": SW, "contract": SWING_POLICY, "policy_version": SWING_POLICY,
        "mode": "SYNTHETIC", "as_of": AS_OF.isoformat(), "price_session": SESSION.isoformat(),
        "status": "READY", "news_checked_count": 3, "candidates": candidates,
    }, observed_at=AS_OF)


def seed_performance(repository):
    records = []
    for report_id in (UR, SW, REGULAR):
        for horizon in (5, 20, 60):
            for index, (symbol, _, decision) in enumerate(ISSUERS[:3]):
                completed = index < 2 or horizon == 5
                gross = Decimal("0.025") + horizon * Decimal("0.0007") - index * Decimal("0.05") if completed else None
                status = PerformanceStatus.COMPLETE if completed else (
                    PerformanceStatus.PENDING_HORIZON if horizon == 20 else PerformanceStatus.PENDING_ENTRY
                )
                records.append(PerformanceRecord(
                    report_id=report_id, ruleset_version="SYNTHETIC_AUDIT_V1", symbol=symbol, decision=decision,
                    strategy="MOMENTUM_CONTINUATION", expected_holding_period="5–60 trading days",
                    score=Decimal(85 - index * 8), signal_at=AS_OF, evaluated_at=AS_OF + timedelta(days=95),
                    trading_days=horizon, status=status, entry_date="2026-11-16", entry_price=Decimal(54_100),
                    exit_date=(SESSION + timedelta(days=horizon + 3)).isoformat() if completed else None,
                    gross_return=gross, net_return=gross - Decimal("0.003") if gross is not None else None,
                    round_trip_cost_bps=Decimal(30),
                ))
    repository.save_performance_records(records)


def seed_stock(runtime):
    repository = StockRepository(runtime / "stock-assistant.sqlite3")
    store = ResearchData(runtime / "unified-research-inputs.sqlite3", source_database=repository.database_path)
    dates = weekdays(SESSION, 246)
    for offset, (symbol, _, _) in enumerate(ISSUERS):
        bars = []
        for index, day in enumerate(dates if offset == 0 else dates[-46:]):
            close = Decimal(43_000 + offset * 4_000 + index * 51 + (index % 17 - 8) * 216)
            opening = close + (-270 if index % 3 else 310)
            bars.append(OHLCV(
                symbol, day, opening, max(opening, close) + 430, min(opening, close) - 345, close,
                330_000 + (index * 79_289) % 950_000, "SYNTHETIC_FIXTURE", datetime.combine(day, time(8), UTC),
            ))
        repository.save_bars(bars)
    repository.replace_holding(Holding("합성 브로커 A", ISSUERS[0][0], Decimal(8), Decimal(49_000), dates[-36]))
    repository.replace_holding(Holding("합성 브로커 B", ISSUERS[0][0], Decimal(5), Decimal(52_000), dates[-18]))
    repository.replace_holding(Holding("합성 브로커 A", ISSUERS[1][0], Decimal(12), Decimal(46_100), dates[-23]))
    repository.save_thesis(ThesisCard(
        "AUDIT-THESIS-1", ISSUERS[0][0], AS_OF, ("합성 분기 영업현금흐름 개선",),
        ("합성 수주 잔고 유지",), ("합성 분기 매출이 계획 하단을 하회",),
        ("다음 합성 공시에서 납기 변경을 확인",), ThesisStatus.APPROVED,
    ))
    repository.save_screening_results(REGULAR, [
        ScreeningResult(symbol, AS_OF, Decision.CANDIDATE, Decimal(81 - index * 7),
                        ("합성 거래대금 조건 충족",), ("감사용 합성 사례",), ("합성 결산 검토",), name=name)
        for index, (symbol, name, _) in enumerate(ISSUERS[:2])
    ])
    seed_reports(store)
    seed_performance(repository)


def seed_operations(root, state, runtime):
    now = datetime.now(UTC)
    usage_path = runtime / "news-api-usage.sqlite3"
    NewsApiBudget.initialize(usage_path, at=now, used_today=640, used_month=12_340)
    with closing(sqlite3.connect(usage_path)) as connection, connection:
        connection.execute("CREATE TABLE news_api_cooldowns (scope TEXT PRIMARY KEY, last_reserved_at TEXT NOT NULL)")
        connection.execute("UPDATE news_api_budget SET last_reserved_at=? WHERE id=1",
                           ((now - timedelta(hours=8)).isoformat(),))
        connection.execute("INSERT INTO news_api_cooldowns VALUES (?, ?)",
                           ("SWING", (now - timedelta(minutes=8)).isoformat()))
    artifacts = root / "artifacts"
    write_json(artifacts / "source-audit.json", {
        "checked_at_kst": now.isoformat(), "source_head": "a" * 40, "all_snapshot_hashes_match": True,
        "real_three_year_replay": "BLOCKED_NO_POINT_IN_TIME_DATASET",
    })
    price_days = weekdays(SESSION, 246)
    price_start = date(2026, 7, 1)
    write_json(artifacts / "oracle-data-coverage.json", {
        "checked_at_kst": now.isoformat(), "mode": "SYNTHETIC", "read_only": True,
        "point_in_time_certified": False, "tables": {
            "ohlcv": {"rows": 430, "symbols": 5, "distinct_dates": 246,
                      "trade_date_range": [price_days[0].isoformat(), SESSION.isoformat()]},
            "financial_snapshots": {"rows": 24, "symbols": 5},
        },
    })
    write_json(artifacts / "observed-price-calculation.json", {
        "as_of": AS_OF.isoformat(), "results": [{
            "symbol": ISSUERS[0][0], "start": price_start.isoformat(), "end": SESSION.isoformat(),
            "distinct_trading_dates": sum(day >= price_start for day in price_days),
            "calculation_dates_checked": 60, "fields_checked": ["close", "sma20", "sma60", "return5"],
            "source_features_equal_independent_formulas": True,
        }],
    })
    write_json(artifacts / "observed-financial-calculation.json", {
        "symbol": ISSUERS[0][0], "division": "CFS", "observed_at": AS_OF.isoformat(), "all_equal": True,
        "checks": {"stored_annual_income_equal": True, "stored_ttm_income_equal": True, "stored_receipt_links_equal": True},
        "values": {"annual_operating_income": "3200000000", "current_cumulative_income": "1800000000",
                   "previous_cumulative_income": "1500000000", "ttm_operating_income": "3500000000"},
    })
    write_json(state / "status.json", {
        "status": "OBSERVING", "at": now.isoformat(), "orders_enabled": False, "entry_blocked": False,
        "unclosed_positions": 2, "active_sockets": 1, "ws_frames": 16_804, "notification_status": "UNAVAILABLE",
        "live_settings": {"max_daily_entries": 3, "strategy_config": {"take_profit_bps": "60"}},
    })


def seed_daytrade(runtime, state):
    template = (ROOT / "engine/tests/fixtures/daytrade_bot_sample.json").read_text(encoding="utf-8")
    for day in weekdays(SESSION, 3):
        day_root = state / "daily" / day.isoformat()
        datasets = day_root / "datasets"
        datasets.mkdir(parents=True)
        (datasets / "daytrade_bot_sample.json").write_text(
            template.replace(SESSION.isoformat() + "T", day.isoformat() + "T"), encoding="utf-8",
        )
        if not simulate_sample_daytrade(day_root)["present"]:
            raise RuntimeError("daytrade audit report was not produced")
    latest = state / "daily" / SESSION.isoformat() / ".runtime" / "intraday-paper.sqlite3"
    read_only_latest = state / "latest" / ".runtime"
    read_only_latest.mkdir(parents=True)
    shutil.copy2(latest, read_only_latest / latest.name)
    shutil.copy2(latest, runtime / latest.name)


def seed_experiments(server):
    fixture = server.root / "datasets/validation.json"
    fixture.parent.mkdir()
    shutil.copyfile(ROOT / "datasets/validation.json", fixture)
    dataset = json.loads(fixture.read_text(encoding="utf-8"))
    if dataset.get("mode") != "SYNTHETIC":
        raise ValueError("experiment audit requires a synthetic dataset")
    for cost in ("10", "60", "30"):
        experiment = server.store.create(dataset, Policy(round_trip_bps=cost))
        server.store.apply(experiment, "start")
        result = server.store.apply(experiment, limit=len(dataset["events"]))
        if result["state"]["status"] != "COMPLETED":
            raise RuntimeError("experiment audit fixture did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=63055)
    parser.add_argument("--readonly-port", type=int, default=63056)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 86_400:
        parser.error("--seconds must be between 1 and 86400")
    dist = ROOT / "web/dist"
    if not (dist / "index.html").is_file():
        parser.error("build dashboard first: npm --prefix dashboard run build")
    with TemporaryDirectory(prefix="finance-phase-b-audit-") as temporary, ExitStack() as cleanup:
        root = Path(temporary)
        runtime = root / ".runtime"
        runtime.mkdir()
        state = runtime / "daytrade"
        os.environ.update(STOCK_DATABASE_PATH=str(runtime / "stock-assistant.sqlite3"),
                          STOCK_UNIFIED_RESEARCH_DATABASE=str(runtime / "unified-research-inputs.sqlite3"),
                          STOCK_NEWS_USAGE_FILE=str(runtime / "news-api-usage.sqlite3"),
                          DAYTRADE_STATE_ROOT=str(state), DAYTRADE_TOKEN_CACHE="")
        shutil.copytree(dist, root / "web/dist")
        seed_stock(runtime)
        seed_operations(root, state, runtime)
        seed_daytrade(runtime, state)

        full = cleanup.enter_context(DashboardServer(root, port=args.port))
        seed_experiments(full)
        experiment_worker = threading.Thread(target=full.run_worker, daemon=True)
        experiment_worker.start()
        cleanup.callback(experiment_worker.join)
        cleanup.callback(full.stopping.set)
        readonly = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", args.readonly_port), ReadOnlyHandler))
        readonly.root, readonly.state_root = root, state
        readonly.sessions, readonly.daytrade_lock = set(), threading.Lock()
        for server in (full, readonly):
            server.synthetic_audit = True
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            cleanup.callback(worker.join)
            cleanup.callback(server.shutdown)
        print(json.dumps({
            "mode": "SYNTHETIC", "seconds": args.seconds, "port": full.server_port,
            "read_only_port": readonly.server_port, "research_report": UR, "swing_report": SW,
            "daytrade_dates": [day.isoformat() for day in weekdays(SESSION, 3)],
        }), flush=True)
        try:
            threading.Event().wait(args.seconds)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
