from __future__ import annotations

import argparse
import json
import sqlite3
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .client import StockClient, StockClientError
from .alerts import AlertStore
from .http_api import serve
from .ingestion import DartDisclosureEnricher, DartFinancialEnricher, KrxHistoryIngestor
from .intraday import build_intraday_signal
from .models import to_json_value
from .news_discovery import NewsDiscoveryStore, discover_news
from .performance import (
    evaluate_all_report_performance,
    evaluate_report_performance,
    summarize_performance,
)
from .pipeline import CandidatePipeline
from .presentation import render_candidate_report
from .providers.http import ProviderError
from .providers.dart import DartClient
from .providers.krx import KrxClient
from .providers.news import NaverNewsClient
from .providers.toss import TossMarketDataClient
from .repository import StockRepository
from .reports import report_to_dict
from .validation import validate_analysis_time


KST = timezone(timedelta(hours=9), "Asia/Seoul")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _isu_case() -> dict:
    path = _project_root() / "tests" / "fixtures" / "isu_petasis_manual_case.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["judgment"] = "ADDITIONAL_CHECK_REQUIRED"
    payload["additional_checks"] = [
        "각 매수일 당시 이용 가능했던 KRX OHLCV와 DART 공시를 적재할 것",
        "공식 계약·실적 재료의 유효기간과 현재 이행 상태를 확인할 것",
        "추가매수 전 영업이익·OCF·희석성 조달·경영진 위험을 다시 검사할 것",
        "수익 구간의 부분매도와 논리 무효화 조건을 사전에 수치화할 것"
    ]
    payload["notice"] = "현재가와 point-in-time 원자료가 없어 매수·매도 판단은 생성하지 않음"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-assistant")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("isu-case")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=9120)
    serve_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    serve_parser.add_argument("--secret-file", type=Path)
    serve_parser.add_argument("--role-secret-file", type=Path)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("report_id")
    report_parser.add_argument("--base-url", default=os.getenv("STOCK_API_BASE_URL", "http://127.0.0.1:9120"))
    report_parser.add_argument("--token-file", type=Path)
    report_parser.add_argument("--format", choices=("json", "text"), default="text")
    holdings_parser = subparsers.add_parser("holdings")
    holdings_parser.add_argument("--base-url", default=os.getenv("STOCK_API_BASE_URL", "http://127.0.0.1:9120"))
    holdings_parser.add_argument("--token-file", type=Path)
    ingest_parser = subparsers.add_parser("ingest-krx")
    ingest_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    ingest_parser.add_argument("--key-file", type=Path)
    ingest_parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    ingest_parser.add_argument("--calendar-days", type=int, default=120)
    ingest_parser.add_argument("--mode", choices=("daily", "backfill"), default="daily")
    candidates_parser = subparsers.add_parser("generate-candidates")
    candidates_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    candidates_parser.add_argument("--as-of", type=datetime.fromisoformat)
    candidates_parser.add_argument("--limit", type=int, default=5)
    candidates_parser.add_argument("--format", choices=("json", "text"), default="text")
    candidates_parser.add_argument("--include-news", action="store_true")
    candidates_parser.add_argument("--news-fetch-failed", action="store_true")
    dart_parser = subparsers.add_parser("enrich-dart")
    dart_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    dart_parser.add_argument("--key-file", type=Path)
    dart_parser.add_argument("--as-of", type=datetime.fromisoformat)
    dart_parser.add_argument("--business-year", type=int, required=True)
    dart_parser.add_argument("--shortlist-limit", type=int, default=30)
    dart_parser.add_argument("--include-news", action="store_true")
    disclosure_parser = subparsers.add_parser("enrich-dart-disclosures")
    disclosure_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    disclosure_parser.add_argument("--key-file", type=Path)
    disclosure_parser.add_argument("--as-of", type=datetime.fromisoformat)
    disclosure_parser.add_argument("--shortlist-limit", type=int, default=30)
    disclosure_parser.add_argument("--include-news", action="store_true")
    news_parser = subparsers.add_parser("discover-news")
    news_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    news_parser.add_argument("--client-id-file", type=Path, required=True)
    news_parser.add_argument("--client-secret-file", type=Path, required=True)
    news_parser.add_argument("--lookback-hours", type=int, default=48)
    news_parser.add_argument("--display", type=int, default=100)
    performance_parser = subparsers.add_parser("evaluate-performance")
    performance_parser.add_argument("report_id")
    performance_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    performance_parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    performance_parser.add_argument("--horizons", default="5,20,60")
    performance_parser.add_argument("--round-trip-cost-bps", type=Decimal, default=Decimal("30"))
    summary_parser = subparsers.add_parser("performance-summary")
    summary_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    summary_parser.add_argument("--report-id")
    all_performance_parser = subparsers.add_parser("evaluate-all-performance")
    all_performance_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    all_performance_parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    all_performance_parser.add_argument("--horizons", default="5,20,60")
    all_performance_parser.add_argument("--round-trip-cost-bps", type=Decimal, default=Decimal("30"))
    intraday_parser = subparsers.add_parser("refresh-intraday")
    intraday_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    intraday_parser.add_argument("--client-id-file", type=Path, required=True)
    intraday_parser.add_argument("--client-secret-file", type=Path, required=True)
    intraday_parser.add_argument("--candidate-limit", type=int, default=5)
    intraday_parser.add_argument("--candle-count", type=int, default=30)
    intraday_parser.add_argument("--symbols", help="comma-separated explicit symbols for a bounded smoke or on-demand query")
    intraday_parser.add_argument("--alerts-only", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "discover-news":
        observed_at = datetime.now(timezone.utc)
        store = None
        try:
            if not 1 <= args.lookback_hours <= 72 or not 1 <= args.display <= 100:
                raise ValueError("invalid news collection limits")
            repository = StockRepository(args.database)
            securities = repository.list_securities()
            if not securities:
                raise ValueError("news discovery requires a stored security universe")
            store = NewsDiscoveryStore(args.database)
            provider = NaverNewsClient(args.client_id_file.read_text(encoding="utf-8").strip(),
                                       args.client_secret_file.read_text(encoding="utf-8").strip())
            articles = provider.collect(observed_at=observed_at, display=args.display)
            # Collection takes time: observations must not be assigned to the past.
            completed_at = max(observed_at, datetime.now(timezone.utc))
            run = discover_news(articles, securities, as_of=completed_at,
                                lookback_hours=args.lookback_hours)
            store.save(run)
            print(json.dumps({"status": run["status"], "article_count": len(articles),
                              "matched_events": len(run["events"]),
                              "filter_counts": run["filter_counts"]}, ensure_ascii=False))
            return 0
        except (OSError, ValueError, ProviderError, sqlite3.Error) as exc:
            code = getattr(exc, "code", "NEWS_STORE_ERROR" if isinstance(exc, sqlite3.Error) else "NEWS_INPUT_ERROR")
            if code == "NEWS_REFRESH_COOLDOWN":
                print(json.dumps({"status": "SKIPPED", "reason": code}))
                return 0
            if store is not None:
                try:
                    store.save({"status": "UNAVAILABLE", "observed_at": datetime.now(timezone.utc).isoformat(),
                                "error_code": code, "events": []})
                except sqlite3.Error:
                    pass  # The scheduled caller also passes --news-fetch-failed.
            print(json.dumps({"status": "UNAVAILABLE", "error_code": code}), file=sys.stderr)
            return 1

    if args.command == "status":
        print(json.dumps({
            "status": "local_mvp",
            "orders_enabled": False,
            "live_market_data": False,
            "required_credentials": ["KRX_AUTH_KEY", "DART_API_KEY"],
            "optional_credentials": ["TOSSINVEST_CLIENT_ID", "TOSSINVEST_CLIENT_SECRET"],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "isu-case":
        print(json.dumps(_isu_case(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "refresh-intraday":
        try:
            client_id = args.client_id_file.read_text(encoding="utf-8").strip()
            client_secret = args.client_secret_file.read_text(encoding="utf-8").strip()
            if not client_id or not client_secret:
                raise ValueError("Toss credential files cannot be empty")
            run_started_at = datetime.now(timezone.utc)
            repository = StockRepository(args.database)
            if args.symbols:
                symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
                if not symbols or len(symbols) > 25 or len(set(symbols)) != len(symbols):
                    raise ValueError("symbols must contain 1 to 25 unique items")
            else:
                symbols = repository.watch_symbols(
                    as_of=run_started_at, candidate_limit=args.candidate_limit,
                )
            if not symbols:
                if not args.alerts_only:
                    print(json.dumps({"observed_at": run_started_at.isoformat(), "signals": [], "status": "NO_WATCH_SYMBOLS"}))
                return 0
            security_names = repository.security_names(symbols)
            provider = TossMarketDataClient(client_id, client_secret)
            quotes = provider.prices(symbols, observed_at=run_started_at)
            repository.save_market_quotes(quotes)
            signals = []
            for quote in quotes:
                candles = provider.candles(
                    quote.symbol, observed_at=quote.observed_at, count=args.candle_count,
                )
                repository.save_intraday_candles(candles)
                signal_observed_at = max(
                    quote.observed_at,
                    *(item.observed_at for item in candles),
                    datetime.now(timezone.utc),
                )
                signals.append(build_intraday_signal(quote, candles, now=signal_observed_at))
            observed_at = max(
                (signal.observed_at for signal in signals),
                default=run_started_at,
            )
            signal_payloads = []
            for signal in signals:
                payload = to_json_value(signal)
                payload["name"] = security_names.get(signal.symbol)
                signal_payloads.append(payload)
            result_payload = {
                "observed_at": observed_at.isoformat(),
                "source": "TOSS_SECURITIES_OPEN_API",
                "symbols": symbols,
                "signals": signal_payloads,
                "orders_enabled": False,
            }
            if args.alerts_only:
                alert_store = AlertStore(args.database)
                messages = []
                for signal in signals:
                    if "ONE_MINUTE_VOLUME_SPIKE" not in signal.warnings:
                        continue
                    if signal.volume_candle_at is None:
                        raise ValueError("volume spike is missing its source candle timestamp")
                    event_key = (
                        f"intraday-volume:{signal.symbol}:"
                        f"{signal.volume_candle_at.strftime('%Y%m%dT%H%MZ')}"
                    )
                    alert = alert_store.issue(
                        event_key=event_key,
                        symbol=signal.symbol,
                        alert_type="ONE_MINUTE_VOLUME_SPIKE",
                        observed_at=signal.observed_at,
                        payload=to_json_value(signal),
                        cooldown_seconds=1800,
                    )
                    if alert is None:
                        continue
                    ratio = f"{signal.volume_ratio:.2f}" if signal.volume_ratio is not None else "확인 불가"
                    local_time = signal.observed_at.astimezone(KST).isoformat(timespec="seconds")
                    candle_time = signal.volume_candle_at.astimezone(KST).isoformat(timespec="minutes")
                    security_name = security_names.get(signal.symbol)
                    security_label = (
                        f"{security_name}({signal.symbol})"
                        if security_name else f"종목명 확인 불가({signal.symbol})"
                    )
                    messages.append(
                        f"장중 거래량 경고\n"
                        f"종목: {security_label}\n"
                        f"현재가: {signal.last_price:,.0f}원\n"
                        f"기준시각: {local_time}\n"
                        f"신선도: {signal.freshness.value} ({signal.age_seconds}초)\n"
                        f"거래량 기준봉: {candle_time}\n"
                        f"최근 완성 1분봉 거래량: 직전 20개 중앙값 대비 {ratio}배\n"
                        f"이 경고는 매수·매도 지시가 아닙니다."
                    )
                if messages:
                    print("\n\n".join(messages))
                return 0
            print(json.dumps(result_payload, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, ProviderError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "serve":
        secret_file = args.secret_file
        if secret_file is None and os.getenv("STOCK_API_SHARED_SECRET_FILE"):
            secret_file = Path(os.environ["STOCK_API_SHARED_SECRET_FILE"])
        role_secret_file = args.role_secret_file
        if role_secret_file is None and os.getenv("STOCK_API_ROLE_SECRET_FILE"):
            role_secret_file = Path(os.environ["STOCK_API_ROLE_SECRET_FILE"])
        serve(
            host=args.host,
            port=args.port,
            database_path=args.database,
            secret_file=secret_file,
            role_secret_file=role_secret_file,
        )
        return 0
    if args.command in {"report", "holdings"}:
        token_file = args.token_file
        if token_file is None and os.getenv("STOCK_API_ROLE_TOKEN_FILE"):
            token_file = Path(os.environ["STOCK_API_ROLE_TOKEN_FILE"])
        if token_file is None:
            print(json.dumps({"error": "role token file is required"}), file=sys.stderr)
            return 2
        client = StockClient(args.base_url, token_file)
        try:
            if args.command == "holdings":
                print(json.dumps({"holdings": client.list_holdings()}, ensure_ascii=False, indent=2))
                return 0
            report = client.get_report(args.report_id)
            if args.format == "json":
                print(json.dumps({"report": report}, ensure_ascii=False, indent=2))
            else:
                print("\n\n".join(render_candidate_report(report)))
            return 0
        except (StockClientError, ValueError) as exc:
            status = exc.status if isinstance(exc, StockClientError) else None
            print(json.dumps({"error": str(exc), "status": status}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "ingest-krx":
        key_file = args.key_file
        if key_file is None and os.getenv("KRX_AUTH_KEY_FILE"):
            key_file = Path(os.environ["KRX_AUTH_KEY_FILE"])
        if key_file is None:
            print(json.dumps({"error": "KRX key file is required"}), file=sys.stderr)
            return 2
        try:
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("KRX key file is empty")
            ingestor = KrxHistoryIngestor(StockRepository(args.database), KrxClient(key))
            observed_at = datetime.now(timezone.utc)
            if args.mode == "daily":
                summary = ingestor.ingest_day(business_date=args.end_date, observed_at=observed_at)
            else:
                summary = ingestor.backfill(
                    end_date=args.end_date,
                    calendar_days=args.calendar_days,
                    observed_at=observed_at,
                )
            print(json.dumps(to_json_value(summary), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, ProviderError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "generate-candidates":
        now = datetime.now(timezone.utc)
        as_of = args.as_of or now
        try:
            as_of = validate_analysis_time(as_of, now=now)
            summary = CandidatePipeline(StockRepository(args.database)).run(
                as_of=as_of, limit=args.limit, include_news=args.include_news or args.news_fetch_failed,
                news_fetch_failed=args.news_fetch_failed,
            )
            if args.format == "json":
                payload = to_json_value(summary)
                payload["report"] = report_to_dict(summary.report)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("\n\n".join(render_candidate_report(report_to_dict(summary.report))))
            return 0
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "enrich-dart":
        key_file = args.key_file
        if key_file is None and os.getenv("DART_API_KEY_FILE"):
            key_file = Path(os.environ["DART_API_KEY_FILE"])
        if key_file is None:
            print(json.dumps({"error": "DART key file is required"}), file=sys.stderr)
            return 2
        observed_at = datetime.now(timezone.utc)
        as_of = args.as_of or observed_at
        try:
            as_of = validate_analysis_time(as_of, now=observed_at)
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("DART key file is empty")
            repository = StockRepository(args.database)
            symbols = CandidatePipeline(repository).enrichment_symbols(
                as_of=as_of, limit=args.shortlist_limit, include_news=args.include_news,
            )
            summary = DartFinancialEnricher(repository, DartClient(key)).enrich(
                symbols=symbols,
                as_of=as_of,
                business_year=args.business_year,
                observed_at=observed_at,
            )
            print(json.dumps(to_json_value(summary), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, ProviderError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "enrich-dart-disclosures":
        key_file = args.key_file
        if key_file is None and os.getenv("DART_API_KEY_FILE"):
            key_file = Path(os.environ["DART_API_KEY_FILE"])
        if key_file is None:
            print(json.dumps({"error": "DART key file is required"}), file=sys.stderr)
            return 2
        observed_at = datetime.now(timezone.utc)
        as_of = args.as_of or observed_at
        try:
            as_of = validate_analysis_time(as_of, now=observed_at)
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("DART key file is empty")
            repository = StockRepository(args.database)
            symbols = CandidatePipeline(repository).enrichment_symbols(
                as_of=as_of, limit=args.shortlist_limit, include_news=args.include_news,
            )
            summary = DartDisclosureEnricher(repository, DartClient(key)).enrich(
                symbols=symbols,
                as_of=as_of,
                observed_at=observed_at,
            )
            print(json.dumps(to_json_value(summary), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, ProviderError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "evaluate-performance":
        try:
            horizons = tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip())
            evaluation = evaluate_report_performance(
                StockRepository(args.database),
                report_id=args.report_id,
                evaluated_at=args.as_of,
                horizons=horizons,
                round_trip_cost_bps=args.round_trip_cost_bps,
            )
            print(json.dumps(to_json_value(evaluation), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "performance-summary":
        try:
            records = StockRepository(args.database).list_performance_records(report_id=args.report_id)
            print(json.dumps({
                "records": to_json_value(records),
                "summary": summarize_performance(records),
            }, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "evaluate-all-performance":
        try:
            horizons = tuple(int(value.strip()) for value in args.horizons.split(",") if value.strip())
            repository = StockRepository(args.database)
            evaluations = evaluate_all_report_performance(
                repository,
                evaluated_at=args.as_of,
                horizons=horizons,
                round_trip_cost_bps=args.round_trip_cost_bps,
            )
            records = repository.list_performance_records()
            print(json.dumps({
                "evaluated_reports": len(evaluations),
                "records": to_json_value(records),
                "summary": summarize_performance(records),
            }, ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
