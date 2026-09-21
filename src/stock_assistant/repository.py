from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    AssetType,
    Catalyst,
    CatalystStatus,
    CompanyKind,
    EtfSnapshot,
    Evidence,
    FinancialCompanySnapshot,
    FinancialSnapshot,
    FinancingEvent,
    Holding,
    ManagementRisk,
    Market,
    OHLCV,
    ScreeningResult,
    Security,
    ThesisCard,
    to_json_value,
)


SCHEMA_VERSION = 7
_REPORT_ID = re.compile(r"^R-[0-9]{8}T[0-9]{4}Z-[A-F0-9]{8}$")
_DATASET = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


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
                CREATE TABLE IF NOT EXISTS financial_company_snapshots (
                    symbol TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, period_end, published_at)
                );
                CREATE TABLE IF NOT EXISTS etf_snapshots (
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, trade_date, observed_at)
                );
                CREATE TABLE IF NOT EXISTS financing_events (
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    announced_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, event_type, announced_at, source_url)
                );
                CREATE TABLE IF NOT EXISTS research_coverage (
                    symbol TEXT NOT NULL,
                    dataset TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, dataset)
                );
                CREATE TABLE IF NOT EXISTS catalysts (
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    announced_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, category, announced_at, source_url)
                );
                CREATE TABLE IF NOT EXISTS management_risks (
                    symbol TEXT NOT NULL,
                    category TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (symbol, category, published_at, source_url)
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
                CREATE TABLE IF NOT EXISTS performance_records (
                    report_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    trading_days INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (report_id, symbol, trading_days)
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

    def list_report_ids(self, *, as_of: datetime | None = None) -> list[str]:
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        query = "SELECT report_id FROM reports"
        parameters: tuple[str, ...] = ()
        if as_of is not None:
            query += " WHERE as_of<=?"
            parameters = (as_of.astimezone(timezone.utc).isoformat(),)
        query += " ORDER BY as_of, report_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [str(row["report_id"]) for row in rows]

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

    def bars_since(self, symbol: str, *, start_date, as_of: datetime) -> list[OHLCV]:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if start_date > as_of.date():
            raise ValueError("start_date cannot be after as_of date")
        as_of_utc = as_of.astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT current.payload_json FROM ohlcv AS current "
                "WHERE current.symbol=? AND current.trade_date>=? AND current.trade_date<=? "
                "AND current.observed_at<=? AND current.observed_at=("
                "SELECT MAX(history.observed_at) FROM ohlcv AS history "
                "WHERE history.symbol=current.symbol AND history.trade_date=current.trade_date "
                "AND history.observed_at<=?"
                ") ORDER BY current.trade_date",
                (symbol, start_date.isoformat(), as_of.date().isoformat(), as_of_utc, as_of_utc),
            ).fetchall()
        return [_bar_from_dict(json.loads(row["payload_json"])) for row in rows]

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

    def save_financial_company_snapshot(self, snapshot: FinancialCompanySnapshot) -> None:
        payload = json.dumps(to_json_value(snapshot), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO financial_company_snapshots(symbol, period_end, published_at, payload_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(symbol, period_end, published_at) DO UPDATE SET "
                "payload_json=excluded.payload_json",
                (snapshot.symbol, snapshot.period_end.isoformat(), snapshot.published_at.isoformat(), payload),
            )

    def latest_financial_company(
        self,
        symbol: str,
        *,
        as_of: datetime,
    ) -> FinancialCompanySnapshot | None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM financial_company_snapshots WHERE symbol=? ORDER BY published_at DESC",
                (symbol,),
            ).fetchall()
        for row in rows:
            snapshot = _financial_company_from_dict(json.loads(row["payload_json"]))
            if snapshot.published_at <= as_of.astimezone(timezone.utc):
                return snapshot
        return None

    def save_etf_snapshots(self, snapshots: list[EtfSnapshot]) -> None:
        with self._connect() as connection:
            for snapshot in snapshots:
                payload = json.dumps(to_json_value(snapshot), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO etf_snapshots(symbol, trade_date, observed_at, payload_json) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(symbol, trade_date, observed_at) DO UPDATE SET "
                    "payload_json=excluded.payload_json",
                    (
                        snapshot.symbol,
                        snapshot.trade_date.isoformat(),
                        snapshot.observed_at.isoformat(),
                        payload,
                    ),
                )

    def latest_etf_snapshot(self, symbol: str, *, as_of: datetime) -> EtfSnapshot | None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM etf_snapshots WHERE symbol=? AND trade_date<=? "
                "AND observed_at<=? ORDER BY trade_date DESC, observed_at DESC",
                (
                    symbol,
                    as_of.date().isoformat(),
                    as_of.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchall()
        return _etf_snapshot_from_dict(json.loads(rows[0]["payload_json"])) if rows else None

    def save_financing_events(self, events: list[FinancingEvent]) -> None:
        with self._connect() as connection:
            for event in events:
                payload = json.dumps(to_json_value(event), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO financing_events(symbol, event_type, announced_at, source_url, payload_json) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, event_type, announced_at, source_url) "
                    "DO UPDATE SET payload_json=excluded.payload_json",
                    (
                        event.symbol,
                        event.event_type,
                        event.announced_at.isoformat(),
                        event.source_url,
                        payload,
                    ),
                )

    def financing_events_for(
        self,
        symbol: str,
        *,
        as_of: datetime,
        since: datetime,
    ) -> list[FinancingEvent]:
        if as_of.tzinfo is None or since.tzinfo is None:
            raise ValueError("as_of and since must be timezone-aware")
        as_of_utc = as_of.astimezone(timezone.utc)
        since_utc = since.astimezone(timezone.utc)
        if since_utc > as_of_utc:
            raise ValueError("since cannot be after as_of")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM financing_events WHERE symbol=? "
                "AND announced_at>=? AND announced_at<=? ORDER BY announced_at, event_type, source_url",
                (symbol, since_utc.isoformat(), as_of_utc.isoformat()),
            ).fetchall()
        return [_financing_from_dict(json.loads(row["payload_json"])) for row in rows]

    def save_coverage(
        self,
        *,
        symbol: str,
        dataset: str,
        start_date: str,
        end_date: str,
        observed_at: datetime,
    ) -> None:
        if not (symbol.isdigit() and len(symbol) == 6):
            raise ValueError("coverage symbol must be six digits")
        if not _DATASET.fullmatch(dataset):
            raise ValueError("coverage dataset has an invalid format")
        from datetime import date
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if start > end:
            raise ValueError("coverage start_date cannot be after end_date")
        if observed_at.tzinfo is None:
            raise ValueError("coverage observed_at must be timezone-aware")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO research_coverage(symbol, dataset, start_date, end_date, observed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, dataset) DO UPDATE SET "
                "start_date=excluded.start_date, end_date=excluded.end_date, observed_at=excluded.observed_at",
                (symbol, dataset, start.isoformat(), end.isoformat(), observed_at.astimezone(timezone.utc).isoformat()),
            )

    def has_coverage(
        self,
        *,
        symbol: str,
        dataset: str,
        required_start_date: str,
        required_end_date: str,
        as_of: datetime,
    ) -> bool:
        if not _DATASET.fullmatch(dataset):
            raise ValueError("coverage dataset has an invalid format")
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM research_coverage WHERE symbol=? AND dataset=? "
                "AND start_date<=? AND end_date>=? AND observed_at<=? LIMIT 1",
                (
                    symbol,
                    dataset,
                    required_start_date,
                    required_end_date,
                    as_of.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchone()
        return row is not None

    def save_catalysts(self, catalysts: list[Catalyst]) -> None:
        with self._connect() as connection:
            for catalyst in catalysts:
                if not catalyst.evidence:
                    raise ValueError("persisted catalyst requires evidence")
                source_url = catalyst.evidence[0].url
                payload = json.dumps(to_json_value(catalyst), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO catalysts(symbol, category, announced_at, source_url, payload_json) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, category, announced_at, source_url) "
                    "DO UPDATE SET payload_json=excluded.payload_json",
                    (catalyst.symbol, catalyst.category, catalyst.announced_at.isoformat(), source_url, payload),
                )

    def catalysts_for(self, symbol: str, *, as_of: datetime, since: datetime) -> list[Catalyst]:
        if as_of.tzinfo is None or since.tzinfo is None:
            raise ValueError("as_of and since must be timezone-aware")
        as_of_utc = as_of.astimezone(timezone.utc)
        since_utc = since.astimezone(timezone.utc)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM catalysts WHERE symbol=? AND announced_at>=? "
                "AND announced_at<=? ORDER BY announced_at, category, source_url",
                (symbol, since_utc.isoformat(), as_of_utc.isoformat()),
            ).fetchall()
        return [_catalyst_from_dict(json.loads(row["payload_json"])) for row in rows]

    def save_management_risks(self, risks: list[ManagementRisk]) -> None:
        with self._connect() as connection:
            for risk in risks:
                if not risk.evidence:
                    raise ValueError("persisted management risk requires evidence")
                published_at = max(item.published_at for item in risk.evidence)
                source_url = risk.evidence[0].url
                payload = json.dumps(to_json_value(risk), ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO management_risks(symbol, category, published_at, source_url, payload_json) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, category, published_at, source_url) "
                    "DO UPDATE SET payload_json=excluded.payload_json",
                    (risk.symbol, risk.category, published_at.isoformat(), source_url, payload),
                )

    def management_risks_for(
        self,
        symbol: str,
        *,
        as_of: datetime,
        since: datetime,
    ) -> list[ManagementRisk]:
        if as_of.tzinfo is None or since.tzinfo is None:
            raise ValueError("as_of and since must be timezone-aware")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM management_risks WHERE symbol=? AND published_at>=? "
                "AND published_at<=? ORDER BY published_at, category, source_url",
                (
                    symbol,
                    since.astimezone(timezone.utc).isoformat(),
                    as_of.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchall()
        return [_management_risk_from_dict(json.loads(row["payload_json"])) for row in rows]

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

    def save_performance_records(self, records) -> None:
        from .performance import PerformanceStatus

        with self._connect() as connection:
            for record in records:
                payload = json.dumps(to_json_value(record), ensure_ascii=False, sort_keys=True)
                existing = connection.execute(
                    "SELECT status, payload_json FROM performance_records "
                    "WHERE report_id=? AND symbol=? AND trading_days=?",
                    (record.report_id, record.symbol, record.trading_days),
                ).fetchone()
                if existing is not None and existing["status"] == PerformanceStatus.COMPLETE.value:
                    previous = _performance_from_dict(json.loads(existing["payload_json"]))
                    stable_fields = (
                        "report_id", "ruleset_version", "symbol", "decision", "strategy",
                        "expected_holding_period", "score",
                        "signal_at", "trading_days", "status", "entry_date", "entry_price",
                        "exit_date", "gross_return", "net_return",
                    )
                    if any(getattr(previous, field) != getattr(record, field) for field in stable_fields):
                        raise ValueError("completed performance record is immutable")
                    continue
                connection.execute(
                    "INSERT INTO performance_records VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(report_id, symbol, trading_days) DO UPDATE SET "
                    "status=excluded.status, evaluated_at=excluded.evaluated_at, payload_json=excluded.payload_json",
                    (
                        record.report_id, record.symbol, record.trading_days,
                        record.status.value, record.evaluated_at.isoformat(), payload,
                    ),
                )

    def list_performance_records(self, *, report_id: str | None = None):
        query = "SELECT payload_json FROM performance_records"
        parameters: tuple[str, ...] = ()
        if report_id is not None:
            if not _REPORT_ID.fullmatch(report_id):
                raise ValueError("report_id has an invalid format")
            query += " WHERE report_id=?"
            parameters = (report_id,)
        query += " ORDER BY report_id, symbol, trading_days"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_performance_from_dict(json.loads(row["payload_json"])) for row in rows]


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
        annual_operating_income=(
            Decimal(payload["annual_operating_income"])
            if payload.get("annual_operating_income") is not None else None
        ),
        ttm_operating_income=(
            Decimal(payload["ttm_operating_income"])
            if payload.get("ttm_operating_income") is not None else None
        ),
        ttm_period_end=(
            date.fromisoformat(payload["ttm_period_end"])
            if payload.get("ttm_period_end") else None
        ),
        ttm_source_url=payload.get("ttm_source_url"),
    )


def _financial_company_from_dict(payload: dict) -> FinancialCompanySnapshot:
    from datetime import date, datetime
    from decimal import Decimal

    def optional_decimal(field: str) -> Decimal | None:
        value = payload.get(field)
        return Decimal(value) if value is not None else None

    return FinancialCompanySnapshot(
        symbol=payload["symbol"],
        period_end=date.fromisoformat(payload["period_end"]),
        published_at=datetime.fromisoformat(payload["published_at"]),
        capital_adequacy_ratio=optional_decimal("capital_adequacy_ratio"),
        return_on_equity=optional_decimal("return_on_equity"),
        non_performing_loan_ratio=optional_decimal("non_performing_loan_ratio"),
        delinquency_ratio=optional_decimal("delinquency_ratio"),
        provision_coverage_ratio=optional_decimal("provision_coverage_ratio"),
        shareholder_return_note=payload.get("shareholder_return_note"),
        source_url=payload["source_url"],
    )


def _etf_snapshot_from_dict(payload: dict) -> EtfSnapshot:
    from datetime import date, datetime
    from decimal import Decimal

    def optional_decimal(field: str) -> Decimal | None:
        value = payload.get(field)
        return Decimal(value) if value is not None else None

    return EtfSnapshot(
        symbol=payload["symbol"],
        trade_date=date.fromisoformat(payload["trade_date"]),
        observed_at=datetime.fromisoformat(payload["observed_at"]),
        nav_per_share=optional_decimal("nav_per_share"),
        net_assets=optional_decimal("net_assets"),
        premium_discount_pct=optional_decimal("premium_discount_pct"),
        tracking_error_pct=optional_decimal("tracking_error_pct"),
        total_expense_ratio_pct=optional_decimal("total_expense_ratio_pct"),
        top10_weight_pct=optional_decimal("top10_weight_pct"),
        source_url=payload["source_url"],
    )


def _financing_from_dict(payload: dict) -> FinancingEvent:
    from datetime import datetime
    from decimal import Decimal
    return FinancingEvent(
        symbol=payload["symbol"],
        event_type=payload["event_type"],
        announced_at=datetime.fromisoformat(payload["announced_at"]),
        dilutive=bool(payload["dilutive"]),
        official=bool(payload["official"]),
        source_url=payload["source_url"],
        dilution_ratio_pct=(
            Decimal(payload["dilution_ratio_pct"])
            if payload.get("dilution_ratio_pct") is not None else None
        ),
        purpose=payload.get("purpose"),
        refixing=bool(payload["refixing"]) if payload.get("refixing") is not None else None,
    )


def _evidence_from_dict(payload: dict) -> Evidence:
    from datetime import datetime
    return Evidence(
        source_type=payload["source_type"],
        title=payload["title"],
        url=payload["url"],
        published_at=datetime.fromisoformat(payload["published_at"]),
        observed_at=datetime.fromisoformat(payload["observed_at"]),
        official=bool(payload["official"]),
        facts=tuple(payload.get("facts", [])),
    )


def _catalyst_from_dict(payload: dict) -> Catalyst:
    from datetime import datetime
    return Catalyst(
        symbol=payload["symbol"],
        category=payload["category"],
        status=CatalystStatus(payload["status"]),
        announced_at=datetime.fromisoformat(payload["announced_at"]),
        valid_until=(
            datetime.fromisoformat(payload["valid_until"])
            if payload.get("valid_until") is not None else None
        ),
        evidence=tuple(_evidence_from_dict(item) for item in payload.get("evidence", [])),
    )


def _management_risk_from_dict(payload: dict) -> ManagementRisk:
    return ManagementRisk(
        symbol=payload["symbol"],
        confirmed=bool(payload["confirmed"]),
        category=payload["category"],
        evidence=tuple(_evidence_from_dict(item) for item in payload.get("evidence", [])),
    )


def _performance_from_dict(payload: dict):
    from decimal import Decimal
    from .performance import PerformanceRecord, PerformanceStatus

    def optional_decimal(field: str):
        value = payload.get(field)
        return Decimal(value) if value is not None else None

    return PerformanceRecord(
        report_id=payload["report_id"],
        ruleset_version=payload["ruleset_version"],
        symbol=payload["symbol"],
        decision=payload["decision"],
        strategy=payload.get("strategy", "UNAVAILABLE"),
        expected_holding_period=payload.get("expected_holding_period", "REVIEW_REQUIRED"),
        score=Decimal(payload["score"]),
        signal_at=datetime.fromisoformat(payload["signal_at"]),
        evaluated_at=datetime.fromisoformat(payload["evaluated_at"]),
        trading_days=int(payload["trading_days"]),
        status=PerformanceStatus(payload["status"]),
        entry_date=payload.get("entry_date"),
        entry_price=optional_decimal("entry_price"),
        exit_date=payload.get("exit_date"),
        gross_return=optional_decimal("gross_return"),
        net_return=optional_decimal("net_return"),
    )
