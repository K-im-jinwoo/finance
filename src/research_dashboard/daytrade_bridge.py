"""Local daytrade reports and opt-in synthetic samples; no live market wiring."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

try:
    from stock_assistant.daytrade import MODE, build_trading_report, render_markdown_report, run_paper_session
except ModuleNotFoundError as exc:
    if exc.name != "stock_assistant":
        raise
    # Resolve relative to this package, never to an untrusted supplied root/CWD.
    engine_source = Path(__file__).resolve().parents[2] / "engine/src"
    if not (engine_source / "stock_assistant/__init__.py").is_file():
        raise
    sys.path.insert(0, str(engine_source))
    from stock_assistant.daytrade import MODE, build_trading_report, render_markdown_report, run_paper_session

from stock_assistant.daytrade.ledger import APPLICATION_ID, DaytradeLedger, FixtureCosts, isolated_path
from stock_assistant.daytrade.market import QualityPolicy
from stock_assistant.daytrade.stream import run_observed_stream


class _ReadOnlyLedger(DaytradeLedger):
    """Use the existing report/audit methods with a genuinely read-only SQLite URI.

    Keep the engine ledger's constructor/execution contract unchanged. Mirror its
    schema and metadata checks here; a read transaction holds a coherent snapshot
    even if a separately invoked local CLI is writing the same ledger.
    """
    def __init__(self, root):
        self.path = isolated_path(root)
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("BEGIN")
            if self.connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("daytrade database identity mismatch")
            self.meta = {key: json.loads(value) for key, value in self.connection.execute("SELECT key,value FROM meta")}
            if self.meta.get("schema") != 1 or self.meta.get("mode") != MODE:
                raise ValueError("unsupported ledger or execution mode")
            self.quality = QualityPolicy(**self.meta["quality"])
            self.costs = FixtureCosts(**self.meta["fixture_costs"]) if self.meta["fixture_costs"] is not None else None
            self.audit()
        except Exception:
            self.connection.close()
            raise


def _sample_path(root):
    # A foreign/temp root must supply its own fixture. No implicit fallback to a
    # different checkout's sample, which would hide an incomplete installation.
    for relative in ("datasets/daytrade_observed_stream_sample.json", "datasets/daytrade_bot_sample.json",
                     "engine/tests/fixtures/daytrade_observed_stream_sample.json", "engine/tests/fixtures/daytrade_bot_sample.json"):
        path = Path(root) / relative
        if path.is_file():
            return path
    return None


def read_daytrade_report(root):
    path = isolated_path(root)
    result = {"present": path.is_file(), "orders_enabled": False, "paper_ready": False,
              "sample_available": _sample_path(root) is not None, "report": None, "markdown": None}
    if not result["present"]:
        return result
    ledger = _ReadOnlyLedger(root)
    try:
        report = build_trading_report(ledger)
        return {**result, "report": report, "markdown": render_markdown_report(report)}
    finally:
        ledger.close()


def simulate_sample_daytrade(root):
    path = isolated_path(root)
    if path.exists():
        raise FileExistsError("daytrade ledger already exists")
    sample = _sample_path(root)
    if sample is None:
        raise FileNotFoundError("daytrade sample unavailable")
    if sample.stat().st_size > 10_000_000:
        raise ValueError("bounded daytrade sample required")
    dataset = json.loads(sample.read_text(encoding="utf-8"))
    runner = run_observed_stream if sample.name == "daytrade_observed_stream_sample.json" else run_paper_session
    runner(dataset, root)
    return read_daytrade_report(root)
