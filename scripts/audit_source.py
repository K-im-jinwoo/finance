import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bootstrap import ROOT

SOURCE = ROOT.parent / "wiki/.work/stock-investment-assistant"
KST = timezone(timedelta(hours=9))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def audit():
    git = ["git", "-c", f"safe.directory={SOURCE.as_posix()}", "-C", str(SOURCE)]
    head = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(git + ["status", "--porcelain"], text=True)
    files = []
    for path in sorted((ROOT / "vendor/stock-assistant").rglob("*")):
        if path.is_file():
            rel = path.relative_to(ROOT / "vendor/stock-assistant")
            files.append({"path": rel.as_posix(), "sha256": sha(path), "source_sha256": sha(SOURCE / rel)})
    dbs = []
    # Aggregate only. Do not instantiate StockRepository (its constructor migrates).
    for path in sorted(SOURCE.rglob("*.sqlite3")):
        if ".venv" in path.parts or ".git" in path.parts:
            continue
        summary = {"path": path.relative_to(SOURCE).as_posix(), "tables": {}}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if not table.replace("_", "").isalnum():
                    raise ValueError("unexpected table name")
                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                item = {"rows": conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]}
                if table in {"ohlcv", "financial_snapshots", "reports", "research_coverage"}:
                    for col in ("trade_date", "period_end", "published_at", "observed_at", "as_of", "start_date", "end_date"):
                        if col in columns:
                            item[col] = list(conn.execute(f'SELECT MIN("{col}"),MAX("{col}") FROM "{table}"').fetchone())
                summary["tables"][table] = item
        dbs.append(summary)
    result = {
        "checked_at_kst": datetime.now(KST).isoformat(), "source_head": head,
        "source_status": status.splitlines(), "snapshot_files": files,
        "all_snapshot_hashes_match": all(f["sha256"] == f["source_sha256"] for f in files),
        "local_database_aggregate_only": dbs,
        "environment_key_presence_only": {key: bool(os.environ.get(key)) for key in ("KRX_AUTH_KEY", "KRX_API_KEY", "DART_API_KEY")},
        "production_db_accessed": False,
        "target_window_proposed": {"start": "2023-09-30", "end": "2026-09-29", "status": "PENDING_DATA"},
        "real_three_year_replay": "BLOCKED_NO_POINT_IN_TIME_DATASET",
    }
    (ROOT / "artifacts/source-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_head": head, "snapshot_files": len(files), "hashes_match": result["all_snapshot_hashes_match"], "db_aggregates": dbs, "key_presence": result["environment_key_presence_only"]}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    audit()

