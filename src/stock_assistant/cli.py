from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .http_api import serve


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
        serve(host=args.host, port=args.port, database_path=args.database, secret_file=secret_file)
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

