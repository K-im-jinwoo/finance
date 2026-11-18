"""Separate local evaluation history and verification tasks; never touches the source DB."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .financial_research import FinancialReview, POLICY_VERSION
from .models import to_json_value
from .validation import validate_analysis_time


APPLICATION_ID = 0x46525331


class FinancialResearchState:
    def __init__(self, path: Path, *, source_database: Path):
        self.path = path.resolve()
        if self.path == source_database.resolve() or (self.path.exists() and self.path.samefile(source_database)):
            raise ValueError("research state database must differ from the source database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"evaluations", "latest_evaluations", "verification_tasks", "metadata"}
            if application_id not in {0, APPLICATION_ID} or (application_id == 0 and tables):
                raise ValueError("existing database is not a financial research state database")
            if application_id == APPLICATION_ID and tables != expected:
                raise ValueError("financial research state schema is incomplete or unexpected")
            connection.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    policy_version TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS latest_evaluations (
                    symbol TEXT NOT NULL, policy_version TEXT NOT NULL, evaluation_id TEXT NOT NULL,
                    PRIMARY KEY(symbol,policy_version));
                CREATE TABLE IF NOT EXISTS verification_tasks (
                    symbol TEXT NOT NULL, policy_version TEXT NOT NULL, reason TEXT NOT NULL,
                    status TEXT NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL, PRIMARY KEY(symbol,policy_version,reason));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.commit()

    def sync(self, reviews: list[FinancialReview], *, as_of: datetime) -> dict:
        as_of = validate_analysis_time(as_of)
        recorded_at = datetime.now(timezone.utc).isoformat()
        if len({review.symbol for review in reviews}) != len(reviews):
            raise ValueError("research reviews must have unique symbols")
        if any(review.as_of != as_of or review.policy_version != POLICY_VERSION for review in reviews):
            raise ValueError("reviews must match the synchronization time and policy")
        changed, reused = 0, 0
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous_time = connection.execute("SELECT value FROM metadata WHERE key='last_sync'").fetchone()
                if previous_time and datetime.fromisoformat(previous_time[0]) > as_of:
                    raise ValueError("research state cannot synchronize backwards in time")
                for review in reviews:
                    previous = connection.execute(
                        "SELECT evaluation_id FROM latest_evaluations WHERE symbol=? AND policy_version=?",
                        (review.symbol, review.policy_version),
                    ).fetchone()
                    if previous and previous[0] == review.evaluation_id:
                        reused += 1
                    else:
                        changed += 1
                    record = to_json_value(review)
                    record["recorded_at"] = recorded_at
                    payload = json.dumps(record, sort_keys=True, ensure_ascii=False)
                    connection.execute("INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?,?)",
                                       (review.evaluation_id, review.symbol, review.fingerprint, review.policy_version, payload))
                    connection.execute("INSERT INTO latest_evaluations VALUES(?,?,?) ON CONFLICT(symbol,policy_version) "
                                       "DO UPDATE SET evaluation_id=excluded.evaluation_id",
                                       (review.symbol, review.policy_version, review.evaluation_id))
                    retained = " AND reason NOT IN (" + ",".join("?" for _ in review.pending_checks) + ")" if review.pending_checks else ""
                    closure = "NOT_REQUIRED" if review.gate in {"EXCLUDED", "NOT_APPLICABLE"} else "RESOLVED"
                    connection.execute("UPDATE verification_tasks SET status=?,updated_at=?,evaluation_id=? "
                                       "WHERE symbol=? AND policy_version=? AND status='OPEN'" + retained,
                                       (closure, recorded_at, review.evaluation_id, review.symbol,
                                        review.policy_version, *review.pending_checks))
                    for reason in review.pending_checks:
                        connection.execute("INSERT INTO verification_tasks VALUES(?,?,?,'OPEN',?,?,?) "
                                           "ON CONFLICT(symbol,policy_version,reason) DO UPDATE SET status='OPEN', "
                                           "opened_at=CASE WHEN verification_tasks.status='OPEN' THEN verification_tasks.opened_at "
                                           "ELSE excluded.opened_at END, "
                                           "updated_at=excluded.updated_at,evaluation_id=excluded.evaluation_id",
                                           (review.symbol, review.policy_version, reason, recorded_at,
                                            recorded_at, review.evaluation_id))
                connection.execute("INSERT INTO metadata VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (as_of.isoformat(),))
                tasks = connection.execute("SELECT status,COUNT(*) FROM verification_tasks GROUP BY status").fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"changed_evaluations": changed, "reused_evaluations": reused,
                "verification_task_counts": dict(tasks), "source_database_changed": False}

