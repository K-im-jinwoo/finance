from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AssetType,
    CompanyKind,
    FinancialSnapshot,
    Holding,
    Market,
    OHLCV,
    ScreeningResult,
    Security,
    ThesisCard,
    to_json_value,
)


SCHEMA_VERSION = 3
_REPORT_ID = re.compile(r"^R-[0-9]{8}T[0-9]{4}Z-[A-F0-9]{8}$")


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
                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS securities (
                    symbol TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ohlcv (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date, observed_at)
                );
                CREATE TABLE IF NOT EXISTS financial_snapshots (
                    symbol TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, period_end, published_at)
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

    def save_report(self, report_id: str, as_of: str, payload: dict) -> None:
        if not _REPORT_ID.fullmatch(report_id):
            raise ValueError("report_id has an invalid format")
        if payload.get("report_id") != report_id:
            raise ValueError("report payload id does not match report_id")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id = ?", (report_id,),
            ).fetchone()
            if existing is not None and existing["payload_json"] != serialized:
                raise ValueError("report_id already exists with different content")
            connection.execute(
                "INSERT INTO reports(report_id, as_of, payload_json) VALUES (?, ?, ?) "
                "ON CONFLICT(report_id) DO NOTHING",
                (report_id, as_of, serialized),
            )

    def get_report(self, report_id: str) -> dict | None:
        if not _REPORT_ID.fullmatch(report_id):
            raise ValueError("report_id has an invalid format")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM reports WHERE report_id = ?", (report_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def save_securities(self, securities: list[Security]) -> None:
        with self._connect() as connection:
            for security in securities:
                payload = json.dumps(to_json_value(security), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO securities(symbol, payload_json) VALUES (?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET payload_json=excluded.payload_json",
                    (security.symbol, payload),
                )

    def list_securities(self) -> list[Security]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM securities ORDER BY symbol",
            ).fetchall()
        return [_security_from_dict(json.loads(row["payload_json"])) for row in rows]

    def save_bars(self, bars: list[OHLCV]) -> None:
        with self._connect() as connection:
            for bar in bars:
                payload = json.dumps(to_json_value(bar), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO ohlcv(symbol, trade_date, observed_at, payload_json) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(symbol, trade_date, observed_at) DO UPDATE SET payload_json=excluded.payload_json",
                    (bar.symbol, bar.trade_date.isoformat(), bar.observed_at.isoformat(), payload),
                )

    def bars_for(self, symbol: str, *, as_of: datetime, limit: int = 120) -> list[OHLCV]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT current.payload_json FROM ohlcv AS current "
                "WHERE current.symbol=? AND current.trade_date<=? AND current.observed_at<=? "
                "AND current.observed_at=("
                "SELECT MAX(history.observed_at) FROM ohlcv AS history "
                "WHERE history.symbol=current.symbol AND history.trade_date=current.trade_date "
                "AND history.observed_at<=?"
                ") ORDER BY current.trade_date DESC LIMIT ?",
                (
                    symbol,
                    as_of.date().isoformat(),
                    as_of.astimezone(timezone.utc).isoformat(),
                    as_of.astimezone(timezone.utc).isoformat(),
                    limit,
                ),
            ).fetchall()
        bars = [_bar_from_dict(json.loads(row["payload_json"])) for row in rows]
        return sorted(bars, key=lambda item: item.trade_date)

    def save_financial_snapshot(self, snapshot: FinancialSnapshot) -> None:
        payload = json.dumps(to_json_value(snapshot), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO financial_snapshots(symbol, period_end, published_at, payload_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(symbol, period_end, published_at) DO UPDATE SET "
                "payload_json=excluded.payload_json",
                (snapshot.symbol, snapshot.period_end.isoformat(), snapshot.published_at.isoformat(), payload),
            )

    def latest_financial(self, symbol: str, *, as_of: datetime) -> FinancialSnapshot | None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM financial_snapshots WHERE symbol=? ORDER BY published_at DESC",
                (symbol,),
            ).fetchall()
        for row in rows:
            snapshot = _financial_from_dict(json.loads(row["payload_json"]))
            if snapshot.published_at <= as_of.astimezone(timezone.utc):
                return snapshot
        return None

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


def _security_from_dict(payload: dict) -> Security:
    from datetime import date
    listed = payload.get("listed_on")
    delisted = payload.get("delisted_on")
    return Security(
        symbol=payload["symbol"],
        name=payload["name"],
        market=Market(payload["market"]),
        asset_type=AssetType(payload["asset_type"]),
        company_kind=CompanyKind(payload["company_kind"]),
        listed_on=date.fromisoformat(listed) if listed else None,
        delisted_on=date.fromisoformat(delisted) if delisted else None,
    )


def _bar_from_dict(payload: dict) -> OHLCV:
    from datetime import date, datetime
    from decimal import Decimal
    return OHLCV(
        symbol=payload["symbol"],
        trade_date=date.fromisoformat(payload["trade_date"]),
        open=Decimal(payload["open"]),
        high=Decimal(payload["high"]),
        low=Decimal(payload["low"]),
        close=Decimal(payload["close"]),
        volume=int(payload["volume"]),
        source=payload["source"],
        observed_at=datetime.fromisoformat(payload["observed_at"]),
    )


def _financial_from_dict(payload: dict) -> FinancialSnapshot:
    from datetime import date, datetime
    from decimal import Decimal
    return FinancialSnapshot(
        symbol=payload["symbol"],
        period_end=date.fromisoformat(payload["period_end"]),
        published_at=datetime.fromisoformat(payload["published_at"]),
        operating_income=Decimal(payload["operating_income"]),
        operating_cash_flow=(
            Decimal(payload["operating_cash_flow"])
            if payload.get("operating_cash_flow") is not None else None
        ),
        free_cash_flow=Decimal(payload["free_cash_flow"]) if payload.get("free_cash_flow") is not None else None,
        receivable_turnover=tuple(Decimal(value) for value in payload.get("receivable_turnover", [])),
        inventory_turnover=tuple(Decimal(value) for value in payload.get("inventory_turnover", [])),
        source_url=payload["source_url"],
    )
