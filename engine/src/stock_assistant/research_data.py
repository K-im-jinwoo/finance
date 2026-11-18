"""Immutable research inputs in a separate local database, never the operating DB."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .models import to_json_value
from .validation import validate_analysis_time

APPLICATION_ID = 0x52534431


class ResearchData:
    def __init__(self, path: Path, *, source_database: Path, read_only=False):
        self.read_only = read_only
        self.path = path.resolve()
        source = source_database.resolve(strict=True)
        if self.path == source or (self.path.exists() and self.path.samefile(source)):
            raise ValueError("research data must differ from the source database")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            app = db.execute("PRAGMA application_id").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if app not in {0, APPLICATION_ID} or (app == 0 and tables) or (
                    app == APPLICATION_ID and tables != {"research_inputs"}):
                raise ValueError("existing database is not a research input database")
            if read_only:
                if app != APPLICATION_ID:
                    raise ValueError("research input database is unavailable")
                return
            db.execute("CREATE TABLE IF NOT EXISTS research_inputs (input_id TEXT PRIMARY KEY, "
                       "kind TEXT NOT NULL, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, "
                       "recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS research_lookup ON research_inputs(kind,symbol,observed_at)")
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.commit()

    def _connect(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) if self.read_only else sqlite3.connect(self.path)
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def recent_unified_candidates(self, *, as_of: datetime, since: datetime) -> list[list[dict]]:
        """Read only $.candidates arrays from recent unified_report rows without loading full blobs."""
        as_of = validate_analysis_time(as_of)
        since = validate_analysis_time(since)
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT json_extract(payload_json, '$.candidates') FROM research_inputs "
                "WHERE kind='unified_report' AND symbol='ALL' "
                "AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?)",
                (since.isoformat(), as_of.isoformat()),
            ).fetchall()
        out = []
        for (raw,) in rows:
            if not raw:
                continue
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                out.append(parsed)
        return out

    def put(self, kind: str, symbol: str, payload: dict, *, observed_at: datetime) -> str:
        return self.put_many([(kind, symbol, payload)], observed_at=observed_at)[0]

    def put_many(self, items, *, observed_at: datetime) -> list[str]:
        if self.read_only:
            raise PermissionError("research input database is read-only")
        observed_at = validate_analysis_time(observed_at)
        records = []
        for kind, symbol, payload in items:
            encoded = json.dumps(to_json_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            identity = hashlib.sha256((kind + "\n" + symbol + "\n" + encoded).encode()).hexdigest()
            records.append((identity, kind, symbol, observed_at.isoformat(), datetime.now(timezone.utc).isoformat(), encoded))
        with closing(self._connect()) as db, db:
            db.executemany("INSERT OR IGNORE INTO research_inputs VALUES(?,?,?,?,?,?)", records)
        return [r[0] for r in records]

    def records(self, kind: str, symbol: str, *, as_of: datetime) -> list[dict]:
        as_of = validate_analysis_time(as_of)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT input_id,observed_at,payload_json FROM research_inputs "
                              "WHERE kind=? AND symbol=? AND julianday(observed_at)<=julianday(?) "
                              "ORDER BY julianday(observed_at),rowid", (kind, symbol, as_of.isoformat())).fetchall()
        return [{"input_id": r[0], "observed_at": r[1], "payload": json.loads(r[2])} for r in rows]

    def latest(self, kind: str, symbol: str, *, as_of: datetime) -> dict | None:
        as_of = validate_analysis_time(as_of)
        with closing(self._connect()) as db:
            row = db.execute("SELECT input_id,observed_at,payload_json FROM research_inputs WHERE kind=? AND symbol=? "
                             "AND julianday(observed_at)<=julianday(?) ORDER BY julianday(observed_at) DESC,rowid DESC LIMIT 1",
                             (kind, symbol, as_of.isoformat())).fetchone()
        return {"input_id": row[0], "observed_at": row[1], "payload": json.loads(row[2])} if row else None

    def latest_by_symbol(self, kind: str, *, as_of: datetime) -> dict:
        as_of = validate_analysis_time(as_of)
        with closing(self._connect()) as db:
            rows = db.execute("SELECT symbol,input_id,observed_at,payload_json FROM (SELECT *, "
                              "ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY julianday(observed_at) DESC,rowid DESC) AS n "
                              "FROM research_inputs WHERE kind=? AND julianday(observed_at)<=julianday(?)) WHERE n=1",
                              (kind, as_of.isoformat())).fetchall()
        return {r[0]: {"input_id": r[1], "observed_at": r[2], "payload": json.loads(r[3])} for r in rows}

    def get(self, input_id: str, *, as_of: datetime) -> dict | None:
        as_of = validate_analysis_time(as_of)
        with closing(self._connect()) as db:
            row = db.execute("SELECT kind,symbol,observed_at,payload_json FROM research_inputs WHERE input_id=? "
                             "AND julianday(observed_at)<=julianday(?)", (input_id, as_of.isoformat())).fetchone()
        return {"kind": row[0], "symbol": row[1], "observed_at": row[2], "payload": json.loads(row[3])} if row else None
