from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .client import StockClient, StockClientError
from .http_api import serve
from .ingestion import DartDisclosureEnricher, DartFinancialEnricher, KrxHistoryIngestor
from .models import to_json_value
from .pipeline import CandidatePipeline
from .presentation import render_candidate_report
from .providers.http import ProviderError
from .providers.dart import DartClient
from .providers.krx import KrxClient
from .repository import StockRepository
from .reports import report_to_dict


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
    dart_parser = subparsers.add_parser("enrich-dart")
    dart_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    dart_parser.add_argument("--key-file", type=Path)
    dart_parser.add_argument("--as-of", type=datetime.fromisoformat)
    dart_parser.add_argument("--business-year", type=int, required=True)
    dart_parser.add_argument("--shortlist-limit", type=int, default=30)
    disclosure_parser = subparsers.add_parser("enrich-dart-disclosures")
    disclosure_parser.add_argument("--database", type=Path, default=Path("data/stock-assistant.sqlite3"))
    disclosure_parser.add_argument("--key-file", type=Path)
    disclosure_parser.add_argument("--as-of", type=datetime.fromisoformat)
    disclosure_parser.add_argument("--shortlist-limit", type=int, default=30)
    args = parser.parse_args(argv)

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
        as_of = args.as_of or datetime.now(timezone.utc)
        try:
            summary = CandidatePipeline(StockRepository(args.database)).run(as_of=as_of, limit=args.limit)
            if args.format == "json":
                print(json.dumps(to_json_value(summary), ensure_ascii=False, indent=2))
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
        as_of = args.as_of or datetime.now(timezone.utc)
        try:
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("DART key file is empty")
            repository = StockRepository(args.database)
            symbols = CandidatePipeline(repository).ranked_symbols(
                as_of=as_of, limit=args.shortlist_limit,
            )
            summary = DartFinancialEnricher(repository, DartClient(key)).enrich(
                symbols=symbols,
                as_of=as_of,
                business_year=args.business_year,
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
        as_of = args.as_of or datetime.now(timezone.utc)
        try:
            key = key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("DART key file is empty")
            repository = StockRepository(args.database)
            symbols = CandidatePipeline(repository).ranked_symbols(
                as_of=as_of, limit=args.shortlist_limit,
            )
            summary = DartDisclosureEnricher(repository, DartClient(key)).enrich(
                symbols=symbols,
                as_of=as_of,
            )
            print(json.dumps(to_json_value(summary), ensure_ascii=False, indent=2))
            return 0
        except (OSError, ValueError, ProviderError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
