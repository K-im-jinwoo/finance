from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import Holding, ScreeningResult, ThesisCard, to_json_value


SCHEMA_VERSION = 1


class StockRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS screening_results (
                    report_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (report_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS holdings (
                    broker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    average_price TEXT NOT NULL,
                    acquired_on TEXT,
                    PRIMARY KEY (broker, symbol)
                );
                CREATE TABLE IF NOT EXISTS thesis_cards (
                    thesis_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def save_screening_results(self, report_id: str, results: list[ScreeningResult]) -> None:
        if not report_id.strip():
            raise ValueError("report_id is required")
        with self._connect() as connection:
            for result in results:
                payload = json.dumps(to_json_value(result), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO screening_results VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(report_id, symbol) DO UPDATE SET as_of=excluded.as_of, payload_json=excluded.payload_json",
                    (report_id, result.symbol, result.as_of.isoformat(), payload),
                )

    def replace_holding(self, holding: Holding) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO holdings VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(broker, symbol) DO UPDATE SET quantity=excluded.quantity, "
                "average_price=excluded.average_price, acquired_on=excluded.acquired_on",
                (
                    holding.broker,
                    holding.symbol,
                    str(holding.quantity),
                    str(holding.average_price),
                    holding.acquired_on.isoformat() if holding.acquired_on else None,
                ),
            )

    def list_holdings(self) -> list[Holding]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT broker, symbol, quantity, average_price, acquired_on FROM holdings ORDER BY symbol, broker"
            ).fetchall()
        from datetime import date
        from decimal import Decimal
        return [
            Holding(
                row["broker"], row["symbol"], Decimal(row["quantity"]), Decimal(row["average_price"]),
                date.fromisoformat(row["acquired_on"]) if row["acquired_on"] else None,
            )
            for row in rows
        ]

    def save_thesis(self, thesis: ThesisCard) -> None:
        payload = json.dumps(to_json_value(thesis), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO thesis_cards VALUES (?, ?, ?) "
                "ON CONFLICT(thesis_id) DO UPDATE SET symbol=excluded.symbol, payload_json=excluded.payload_json",
                (thesis.thesis_id, thesis.symbol, payload),
            )
