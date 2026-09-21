from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .client import StockClient, StockClientError
from .http_api import serve
from .presentation import render_candidate_report


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
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
