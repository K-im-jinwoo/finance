from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{base_url.rstrip('/')}{path}", data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _candidate_payload() -> dict[str, Any]:
    start = date(2026, 1, 1)
    observed = datetime(2026, 9, 20, 11, tzinfo=timezone.utc)
    bars = []
    for index in range(80):
        close = Decimal("100000") + Decimal(index * 1000)
        bars.append({
            "symbol": "005930",
            "trade_date": (start + timedelta(days=index)).isoformat(),
            "open": str(close - Decimal("500")),
            "high": str(close + Decimal("1000")),
            "low": str(close - Decimal("1000")),
            "close": str(close),
            "volume": 300000 if index == 79 else 100000,
            "source": "DEPLOYMENT_SMOKE_FIXTURE",
            "observed_at": observed.isoformat(),
        })
    item = {
        "as_of": "2026-09-20T12:00:00+00:00",
        "security": {
            "symbol": "005930",
            "name": "deployment-smoke",
            "market": "KOSPI",
            "asset_type": "COMMON",
            "company_kind": "GENERAL",
            "listed_on": "1975-06-11",
            "delisted_on": None,
        },
        "bars": bars,
        "financial": {
            "symbol": "005930",
            "period_end": "2025-12-31",
            "published_at": "2026-08-14T00:00:00+00:00",
            "operating_income": "100",
            "operating_cash_flow": "80",
            "free_cash_flow": "60",
            "receivable_turnover": ["8", "8.1"],
            "inventory_turnover": ["6", "6.2"],
            "source_url": "https://opendart.fss.or.kr/deployment-smoke",
            "annual_operating_income": "100",
            "ttm_operating_income": "120",
            "ttm_period_end": "2026-06-30",
            "ttm_source_url": "https://opendart.fss.or.kr/deployment-smoke",
        },
        "financing_events": [],
        "management_risks": [],
        "catalysts": [],
    }
    return {"items": [item], "limit": 1, "facts": ["deployment smoke fixture"]}


def run(base_url: str, role_secret_file: Path) -> dict[str, Any]:
    roles = json.loads(role_secret_file.read_text(encoding="utf-8"))
    required = {"cio", "market", "fundamentals", "risk", "scheduler"}
    if set(roles) != required or any(not isinstance(value, str) or len(value) < 32 for value in roles.values()):
        raise ValueError("role-secret file does not satisfy the deployment contract")

    checks: dict[str, bool] = {}
    status, body = _request(base_url, "GET", "/health")
    checks["health"] = status == 200 and body.get("orders_enabled") is False
    status, _ = _request(base_url, "GET", "/v1/holdings")
    checks["unauthenticated_holdings_401"] = status == 401
    status, _ = _request(base_url, "POST", "/v1/orders", token=roles["cio"], payload={})
    checks["orders_403"] = status == 403
    status, _ = _request(base_url, "GET", "/v1/holdings", token=roles["market"])
    checks["specialist_holdings_403"] = status == 403
    status, body = _request(
        base_url, "POST", "/v1/candidates", token=roles["cio"], payload=_candidate_payload(),
    )
    report = body.get("report", {}) if status == 200 else {}
    report_id = str(report.get("report_id", ""))
    checks["cio_candidate_report"] = status == 200 and report_id.startswith("R-")
    if checks["cio_candidate_report"]:
        status, fetched = _request(
            base_url, "GET", f"/v1/reports/{report_id}", token=roles["market"],
        )
        checks["specialist_same_report"] = (
            status == 200 and fetched.get("report", {}).get("report_id") == report_id
        )
    else:
        checks["specialist_same_report"] = False
    if not all(checks.values()):
        raise RuntimeError("candidate smoke failed: " + ", ".join(
            name for name, passed in checks.items() if not passed
        ))
    return {"status": "passed", "checks": checks, "report_id": report_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:9120")
    parser.add_argument("--role-secret-file", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.base_url, args.role_secret_file), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
