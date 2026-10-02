"""Aggregate-only point-in-time inventory. Never migrates or copies source DB."""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bootstrap import ROOT

DATA_TABLES = {"ohlcv": "trade_date", "financial_snapshots": "period_end", "financial_company_snapshots": "period_end", "etf_snapshots": "trade_date", "financing_events": "announced_at", "catalysts": "announced_at", "management_risks": "published_at", "research_coverage": "start_date", "reports": "as_of"}

def inventory(path: Path, start="2023-09-30", end="2026-09-29"):
    path = path.resolve(strict=True)
    result = {"checked_at_kst": datetime.now(timezone(timedelta(hours=9))).isoformat(), "requested_window_proposed": [start,end], "read_only": True, "tables": {}, "point_in_time_certified": False,
              "limitations": ["Date range and row count do not prove complete trading sessions", "Need immutable revision lineage and availability audit", "Holdings/credentials/logs not queried"]}
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, field in DATA_TABLES.items():
            if table not in tables:
                result["tables"][table] = {"state": "MISSING_TABLE"}
                continue
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            summary = {"rows": conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], "available_time_fields": [key for key in ("observed_at","published_at","announced_at","as_of") if key in columns]}
            if field in columns:
                summary["support_range"] = list(conn.execute(f'SELECT MIN("{field}"),MAX("{field}") FROM "{table}"').fetchone())
                if "symbol" in columns:
                    summary["per_symbol"] = [{"symbol":row[0],"rows":row[1],"distinct_periods":row[2],"start":row[3],"end":row[4]} for row in conn.execute(f'SELECT symbol,COUNT(*),COUNT(DISTINCT "{field}"),MIN("{field}"),MAX("{field}") FROM "{table}" GROUP BY symbol ORDER BY symbol')]
            if table.startswith("financial") and "observed_at" not in columns:
                summary["gap"] = "OBSERVED_AT_AND_REVISION_LINEAGE_REQUIRE_EXTERNAL_ENVELOPE"
            summary["state"] = "EMPTY" if summary["rows"] == 0 else "INVENTORY_ONLY_NOT_CERTIFIED"
            result["tables"][table] = summary
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path",type=Path)
    args = parser.parse_args()
    result = inventory(args.path)
    (ROOT/"artifacts/market-data-coverage.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))

