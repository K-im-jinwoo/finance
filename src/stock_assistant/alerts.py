from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: str
    event_key: str
    symbol: str
    alert_type: str
    observed_at: datetime
    status: DeliveryStatus
    attempt_count: int
    last_error_code: str | None = None


class AlertStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    delivered_at TEXT
                )
                """
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def issue(
        self,
        *,
        event_key: str,
        symbol: str,
        alert_type: str,
        observed_at: datetime,
        payload: dict,
        cooldown_seconds: int = 1800,
    ) -> Alert | None:
        if not event_key.strip() or not alert_type.strip():
            raise ValueError("event_key and alert_type are required")
        if not (symbol.isdigit() and len(symbol) == 6):
            raise ValueError("symbol must be six digits")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        observed = observed_at.astimezone(timezone.utc)
        payload_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        alert_id = f"A-{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:12].upper()}"

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM alerts WHERE event_key = ?", (event_key,),
            ).fetchone()
            if existing is not None:
                return None
            cutoff = (observed - timedelta(seconds=cooldown_seconds)).isoformat()
            recent = connection.execute(
                "SELECT 1 FROM alerts WHERE symbol = ? AND alert_type = ? AND observed_at >= ? LIMIT 1",
                (symbol, alert_type, cutoff),
            ).fetchone()
            if recent is not None:
                return None
            connection.execute(
                "INSERT INTO alerts(alert_id,event_key,symbol,alert_type,observed_at,payload_hash,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (alert_id, event_key, symbol, alert_type, observed.isoformat(), payload_hash, DeliveryStatus.PENDING.value),
            )
        return Alert(alert_id, event_key, symbol, alert_type, observed, DeliveryStatus.PENDING, 0)

    def mark_sent(self, alert_id: str, delivered_at: datetime) -> None:
        if delivered_at.tzinfo is None:
            raise ValueError("delivered_at must be timezone-aware")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE alerts SET status=?, attempt_count=attempt_count+1, last_error_code=NULL, delivered_at=? "
                "WHERE alert_id=?",
                (DeliveryStatus.SENT.value, delivered_at.astimezone(timezone.utc).isoformat(), alert_id),
            ).rowcount
        if changed != 1:
            raise ValueError("alert not found")

    def mark_failed(self, alert_id: str, error_code: str) -> None:
        if not error_code.strip():
            raise ValueError("error_code is required")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE alerts SET status=?, attempt_count=attempt_count+1, last_error_code=? WHERE alert_id=?",
                (DeliveryStatus.FAILED.value, error_code, alert_id),
            ).rowcount
        if changed != 1:
            raise ValueError("alert not found")

    def get(self, alert_id: str) -> Alert | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if row is None:
            return None
        return Alert(
            row["alert_id"], row["event_key"], row["symbol"], row["alert_type"],
            datetime.fromisoformat(row["observed_at"]), DeliveryStatus(row["status"]),
            int(row["attempt_count"]), row["last_error_code"],
        )

