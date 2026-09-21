from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


class ApprovalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PendingApproval:
    approval_id: str
    purpose: str
    user_id: str
    payload_hash: str
    expires_at: datetime


def canonical_payload_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalStore:
    def __init__(
        self,
        database_path: Path,
        *,
        ttl_seconds: int = 600,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self.database_path = database_path
        self.ttl_seconds = ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.token_factory = token_factory or (lambda: f"J-{secrets.token_hex(6).upper()}")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
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

    def issue(self, *, purpose: str, user_id: str, payload: dict) -> PendingApproval:
        if not purpose.strip() or not user_id.strip():
            raise ApprovalError("purpose and user_id are required")
        current = self.now().astimezone(timezone.utc)
        approval = PendingApproval(
            self.token_factory(), purpose, user_id, canonical_payload_hash(payload),
            current + timedelta(seconds=self.ttl_seconds),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    approval.approval_id,
                    approval.purpose,
                    approval.user_id,
                    approval.payload_hash,
                    approval.expires_at.isoformat(),
                ),
            )
        return approval

    def consume(self, approval_id: str, *, purpose: str, user_id: str, payload: dict) -> PendingApproval:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError("approval not found")
            if row["consumed_at"] is not None:
                raise ApprovalError("approval already consumed")
            if row["purpose"] != purpose or row["user_id"] != user_id:
                raise ApprovalError("approval scope mismatch")
            if row["payload_hash"] != canonical_payload_hash(payload):
                raise ApprovalError("approved payload has changed")
            expires_at = datetime.fromisoformat(row["expires_at"]).astimezone(timezone.utc)
            current = self.now().astimezone(timezone.utc)
            if current > expires_at:
                raise ApprovalError("approval expired")
            connection.execute(
                "UPDATE approvals SET consumed_at = ? WHERE approval_id = ? AND consumed_at IS NULL",
                (current.isoformat(), approval_id),
            )
        return PendingApproval(approval_id, purpose, user_id, row["payload_hash"], expires_at)
