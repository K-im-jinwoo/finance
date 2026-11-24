"""Dedicated SQLite observation ledger with opt-in SYNTHETIC fixture execution."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
import os
from pathlib import Path
import sqlite3

from . import MODE, POLICY_VERSION
from .contracts import KST, canonical, digest, identifier, instant, number, symbol
from .market import QualityPolicy, normalize
from .risk import RiskState, Phase

APPLICATION_ID = 0x44595431


@dataclass(frozen=True)
class FixtureCosts:
    """Caller-supplied assumptions, never an approved real fee/tax schedule."""
    version: str
    buy_fee_bps: str
    sell_fee_bps: str
    sell_tax_bps: str
    extra_slippage_bps: str
    participation: str
    latency_ms: int

    def __post_init__(self):
        identifier(self.version)
        for value in (self.buy_fee_bps, self.sell_fee_bps, self.sell_tax_bps, self.extra_slippage_bps):
            if number(value) >= 10000:
                raise ValueError("fixture rate must be below 10000 bps")
        if not 0 < number(self.participation) <= 1 or type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("explicit participation and latency required")

    def charge(self, gross, rate):
        return (gross * Decimal(rate) / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class _FillQuote:
    quantity: int
    gross: Decimal
    fee: Decimal
    tax: Decimal


def _affordable_shares(costs, gross, unit_price, available, budget):
    """Floor to the largest whole-share count including the rounded fill fee."""
    lower, upper = 0, available
    while lower < upper:
        shares = (lower + upper + 1) // 2
        new_gross = gross + shares * unit_price
        if new_gross + costs.charge(new_gross, costs.buy_fee_bps) <= budget:
            lower = shares
        else:
            upper = shares - 1
    return lower


def _price_book_fill(levels, *, side, requested, costs, buy_budget=None):
    participation = Decimal(costs.participation)
    slippage = Decimal(costs.extra_slippage_bps) / 10000
    multiplier = 1 + slippage if side == "BUY" else 1 - slippage
    filled, gross = 0, Decimal(0)
    for level in levels:
        depth = int((Decimal(level["volume"]) * participation).to_integral_value(rounding=ROUND_DOWN))
        available = min(requested - filled, depth)
        price = Decimal(level["price"]) * multiplier
        shares = available if buy_budget is None else _affordable_shares(costs, gross, price, available, buy_budget)
        filled += shares
        gross += shares * price
        # The rest of the asks cannot improve the price of a budget-limited fill.
        if filled == requested or shares < available:
            break
    fee = costs.charge(gross, costs.buy_fee_bps if side == "BUY" else costs.sell_fee_bps)
    tax = costs.charge(gross, costs.sell_tax_bps) if side == "SELL" else Decimal(0)
    return _FillQuote(filled, gross, fee, tax)


def isolated_path(root, path=None):
    root = Path(root).resolve(strict=True)
    target = root / ".runtime" / "intraday-paper.sqlite3"
    supplied = Path(path).absolute() if path is not None else target
    if supplied != target or target.parent.is_symlink() or target.is_symlink():
        raise ValueError("isolated .runtime/intraday-paper.sqlite3 path required")
    if target.parent.exists() and target.parent.resolve() != target.parent:
        raise ValueError("runtime directory must not redirect to another ledger")
    if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
        raise ValueError("linked or non-file ledger refused")
    return target


class DaytradeLedger:
    @classmethod
    def create(cls, root, *, path=None, initial_capital="10000000", quality=None,
               policy_version=POLICY_VERSION, synthetic=False, costs=None, replay_seed=None):
        target = isolated_path(root, path)
        capital = number(initial_capital, positive=True)
        identifier(policy_version)
        if replay_seed is not None and (type(replay_seed) is not int or replay_seed < 0):
            raise ValueError("invalid replay seed")
        if type(synthetic) is not bool or (costs is not None and (not synthetic or not isinstance(costs, FixtureCosts))):
            raise ValueError("execution assumptions are permitted only in synthetic fixtures")
        quality = quality or QualityPolicy()
        if not isinstance(quality, QualityPolicy):
            raise ValueError("quality policy required")
        target.parent.mkdir(mode=0o700, exist_ok=True)
        # Atomic exclusive creation: never truncate or initialize an existing DB.
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        c = sqlite3.connect(target)
        try:
            with c:
                c.execute(f"PRAGMA application_id={APPLICATION_ID}")
                c.executescript("""
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                    CREATE TABLE events (id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE,
                        fingerprint TEXT NOT NULL, at TEXT NOT NULL, kind TEXT NOT NULL,
                        payload TEXT NOT NULL, outcome TEXT NOT NULL);
                    CREATE TABLE observations (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE paper_fills (event_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
                        symbol TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                        gross TEXT NOT NULL, fee TEXT NOT NULL, tax TEXT NOT NULL, payload TEXT NOT NULL);
                """)
                meta = {"mode": MODE, "policy_version": policy_version, "synthetic": synthetic,
                        "initial_capital": str(capital), "quality": asdict(quality),
                        "fixture_costs": asdict(costs) if costs is not None else None, "schema": 1,
                        "replay_seed": replay_seed}
                c.executemany("INSERT INTO meta VALUES(?,?)", [(k, canonical(v)) for k, v in meta.items()])
                state = {"cash": str(capital), "positions": {}, "marks": {}, "intents": {},
                         "used_quotes": [], "exit_requests": {}, "runtime_groups": {}, "sessions": {},
                         "risk": RiskState().record(), "last_sequence": -1, "last_monotonic": 0}
                c.execute("INSERT INTO state VALUES(1,?)", (canonical(state),))
        finally:
            c.close()
        return cls(root, path=target)

    def __init__(self, root, *, path=None):
        self.path = isolated_path(root, path)
        if not self.path.is_file():
            raise ValueError("existing daytrade ledger required")
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True)
        try:
            if self.connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise ValueError("daytrade database identity mismatch")
            self.meta = {k: json.loads(v) for k, v in self.connection.execute("SELECT key,value FROM meta")}
            if self.meta.get("schema") != 1 or self.meta.get("mode") != MODE:
                raise ValueError("unsupported ledger or execution mode")
            self.quality = QualityPolicy(**self.meta["quality"])
            self.costs = FixtureCosts(**self.meta["fixture_costs"]) if self.meta["fixture_costs"] is not None else None
            self.audit()
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def state(self):
        return json.loads(self.connection.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])

    def _usable_book(self, record, at):
        if record is None or record["kind"] != "orderbook" or record["issues"] or record["source_timestamp"] is None:
            return False
        age = (instant(at) - instant(record["source_timestamp"])).total_seconds()
        return (self.quality.quote_max_age_seconds is not None and 0 <= age <= self.quality.quote_max_age_seconds
                and instant(record["received_at_utc"]) <= instant(at))

    def _valuation(self, state, at):
        values, fees = {}, Decimal(0)
        for code, quantity in state["positions"].items():
            if not quantity:
                continue
            record = state["marks"].get(code)
            if self.costs is None or not self._usable_book(record, at):
                return {"status": "MARK_UNCERTAIN", "equity": None, "position_values": {}}
            bid = Decimal(json.loads(record["payload_json"])["bids"][0]["price"])
            value = bid * quantity
            values[code] = value
            slippage = value * Decimal(self.costs.extra_slippage_bps) / 10000
            liquidation_gross = value - slippage
            fees += slippage + self.costs.charge(liquidation_gross, self.costs.sell_fee_bps) + self.costs.charge(liquidation_gross, self.costs.sell_tax_bps)
        return {"status": "KNOWN", "equity": Decimal(state["cash"]) + sum(values.values(), Decimal(0)) - fees,
                "position_values": values, "estimated_liquidation_cost": fees}

    def valuation(self, at):
        return self._valuation(self.state(), instant(at))

    def _audit(self, state):
        expected_cash = Decimal(self.meta["initial_capital"])
        quantities = {}
        for code, side, qty, gross, fee, tax in self.connection.execute("SELECT symbol,side,quantity,gross,fee,tax FROM paper_fills ORDER BY event_id"):
            notional, charges = Decimal(gross), Decimal(fee) + Decimal(tax)
            expected_cash += notional - charges if side == "SELL" else -notional - charges
            quantities[code] = quantities.get(code, 0) + (qty if side == "BUY" else -qty)
        actual = {k: v for k, v in state["positions"].items() if v}
        expected = {k: v for k, v in quantities.items() if v}
        if expected_cash != Decimal(state["cash"]) or expected_cash < 0 or actual != expected or any(v < 0 for v in quantities.values()):
            raise ValueError("cash or quantity identity violated")
        return {"cash_identity": True, "quantity_identity": True, "expected_cash": str(expected_cash)}

    def audit(self):
        return self._audit(self.state())

    def snapshot(self):
        return {"meta": self.meta, "state": self.state(),
                "events": [list(r) for r in self.connection.execute("SELECT id,sequence,fingerprint,at,kind,payload,outcome FROM events ORDER BY sequence")],
                "fills": [json.loads(r[0]) for r in self.connection.execute("SELECT payload FROM paper_fills ORDER BY event_id")]}

    @property
    def state_hash(self):
        return digest(self.snapshot())

    def apply(self, envelope):
        event = json.loads(canonical(envelope))
        event_id = identifier(event["id"])
        if event.get("policy_version") != self.meta["policy_version"]:
            raise ValueError("policy version mismatch")
        seq = event["sequence"]
        if type(seq) is not int or seq < 0 or not isinstance(event.get("data"), dict):
            raise ValueError("explicit sequence and object data required")
        at = instant(event["at"])
        event["at"] = at.isoformat()
        fingerprint = digest(event)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self.connection.execute("SELECT fingerprint FROM events WHERE id=?", (event_id,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise ValueError("conflicting event identity")
                return {"duplicate": True}
            state = self.state()
            if seq <= state["last_sequence"]:
                raise ValueError("event sequence regressed")
            risk = RiskState.restore(state["risk"])
            previous_date = risk.session_date
            risk.advance(at, has_positions=bool(state["positions"]),
                         opening_equity=state["cash"] if not state["positions"] else None)
            if previous_date is not None and previous_date != risk.session_date and state["positions"]:
                state["sessions"][previous_date] = "UNFLATTENED"
            kind, data = event["kind"], event["data"]
            outcome = {"mode": MODE, "paper_ready": False}
            if kind == "SESSION":
                if type(data.get("normal_session_verified")) is not bool:
                    raise ValueError("normal session verification required")
                risk.calendar_verified = data["normal_session_verified"]
                state["sessions"].setdefault(risk.session_date, "OBSERVING")
            elif kind == "MARKET":
                if data["received_monotonic"] < state["last_monotonic"]:
                    raise ValueError("observation monotonic time regressed")
                observation = normalize(data["feed"], data["payload"], event_id=event_id, received_at=at,
                    received_monotonic=data["received_monotonic"], session_id=risk.session_date,
                    issuer=data.get("symbol", "ALL"), source=data.get("source"), quality=self.quality)
                record = observation.record()
                self.connection.execute("INSERT INTO observations VALUES(?,?)", (event_id, canonical(record)))
                state["last_monotonic"] = observation.received_monotonic
                if observation.kind == "orderbook":
                    state["marks"][observation.symbol] = record
                if observation.issues:
                    risk.lock("DATA_DEGRADED", observation.kind + ":" + observation.symbol)
                outcome["quality_issues"] = observation.issues
            elif kind == "SIGNAL":
                if data.get("entry_score") is not None or data.get("exit_score") is not None:
                    raise ValueError("score policy is not finalized")
                outcome["status"] = "OBSERVE_ONLY"
            elif kind == "INTENT":
                if not self.meta["synthetic"]:
                    raise ValueError("OBSERVE_ONLY prohibits execution intents")
                side, code, quantity = data["side"], symbol(data["symbol"]), data["quantity"]
                if side not in {"BUY", "SELL"} or type(quantity) is not int or quantity <= 0:
                    raise ValueError("valid side and positive integer quantity required")
                if side == "BUY" and not risk.candidate_entry_allowed:
                    raise ValueError("entry is blocked by time, calendar or risk locks")
                intent_id = identifier(data.get("intent_id", event_id))
                if intent_id in state["intents"]:
                    raise ValueError("intent already exists")
                state["intents"][intent_id] = {"symbol": code, "side": side, "quantity": quantity, "filled": 0, "at": at.isoformat()}
            elif kind == "FILL":
                outcome.update(self._fixture_fill(event_id, data, state, risk, at))
            elif kind == "LOCK":
                risk.lock(data["lock"], data["reason"])
            elif kind == "RUNTIME_STATUS":
                from .http import RateGroup
                group = str(RateGroup(data["group"]))
                state["runtime_groups"][group] = data["group_state"]
                if data["group_state"]["degraded"]:
                    risk.lock("RATE_LIMIT_DEGRADED", group)
                if data["result"]["status"] != "OK":
                    risk.lock("DATA_DEGRADED", "HTTP:" + group)
            elif kind == "RECOVER":
                risk.recover(data["lock"], data["reason"], verified=data.get("verified") is True,
                             has_positions=bool(state["positions"]))
            elif kind == "END_SESSION":
                if risk.phase != Phase.CLOSED or at.astimezone(KST).hour < 15:
                    raise ValueError("end-session requires verified time progression to close")
                status = "UNFLATTENED" if state["positions"] else "CLOSED"
                state["sessions"][risk.session_date] = status
                if state["positions"]:
                    risk.lock("PRIOR_DAY_UNFLATTENED", risk.session_date)
                outcome["status"] = status
            elif kind != "CLOCK":
                raise ValueError("unsupported ledger event")
            valuation = self._valuation(state, at)
            risk.mark(valuation["equity"])
            session_ended = risk.phase == Phase.CLOSED and at.astimezone(KST).hour >= 15
            if session_ended:
                state["sessions"][risk.session_date] = "UNFLATTENED" if state["positions"] else "CLOSED"
                if state["positions"]:
                    risk.lock("PRIOR_DAY_UNFLATTENED", risk.session_date)
            for code in state["positions"]:
                reasons = []
                if risk.phase in {Phase.TIME_EXITING, Phase.CLOSED}:
                    reasons.append("TIME_EXIT")
                if "LOSS_LOCKED" in risk.locks:
                    reasons.append("LOSS_EXIT")
                if reasons:
                    request = state["exit_requests"].setdefault(risk.session_date + ":" + code,
                        {"symbol": code, "requested_at": at.isoformat(), "reasons": [], "status": "PENDING"})
                    request["reasons"] = sorted(set(request["reasons"] + reasons))
                    if session_ended:
                        request["status"] = "UNFLATTENED"
            for request in state["exit_requests"].values():
                if request["symbol"] not in state["positions"]:
                    request["status"] = "RESOLVED"
            state["risk"], state["last_sequence"] = risk.record(), seq
            self._audit(state)
            self.connection.execute("UPDATE state SET payload=? WHERE id=1", (canonical(state),))
            self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                (event_id, seq, fingerprint, at.isoformat(), kind, canonical(event), canonical(outcome)))
        return outcome

    def _strategy_buy_intent(self, intent_id):
        # The bot uses the creating event's ID. Its stored signal also identifies existing intents after reopen.
        row = self.connection.execute("SELECT payload FROM events WHERE id=? AND kind='INTENT'", (intent_id,)).fetchone()
        signal = json.loads(row[0])["data"].get("strategy_signal") if row else None
        return isinstance(signal, dict) and signal.get("action") == "BUY"

    def _fixture_fill(self, event_id, data, state, risk, at):
        if not self.meta["synthetic"] or self.costs is None:
            raise ValueError("only explicit synthetic fixtures with costs can execute")
        if risk.phase == Phase.CLOSED:
            return {"status": "UNFILLED_SESSION_CLOSED"}
        intent_id = identifier(data["intent_id"])
        intent = state["intents"][intent_id]
        if intent["side"] == "BUY" and not risk.candidate_entry_allowed:
            raise ValueError("new entries blocked; exit monitoring remains available")
        if instant(intent["at"]).astimezone(KST).date() != at.astimezone(KST).date():
            raise ValueError("prior-session intent cannot execute")
        quote_id = identifier(data["quote_event_id"])
        row = self.connection.execute("SELECT payload FROM observations WHERE event_id=?", (quote_id,)).fetchone()
        if row is None:
            raise ValueError("observed orderbook required")
        quote = json.loads(row[0])
        if state["marks"].get(intent["symbol"], {}).get("event_id") != quote_id:
            raise ValueError("latest observed book required")
        earliest = instant(intent["at"]) + timedelta(milliseconds=self.costs.latency_ms)
        if (quote["symbol"] != intent["symbol"] or not self._usable_book(quote, at)
                or instant(quote["source_timestamp"]) < earliest
                or instant(quote["received_at_utc"]) < earliest):
            raise ValueError("fresh post-intent post-latency book required")
        if quote_id in state["used_quotes"]:
            return {"status": "UNFILLED_QUOTE_ALREADY_CONSUMED"}
        code, side = intent["symbol"], intent["side"]
        left = intent["quantity"] - intent["filled"]
        if side == "SELL":
            left = min(left, state["positions"].get(code, 0))
        levels = json.loads(quote["payload_json"])["asks" if side == "BUY" else "bids"]
        priced = _price_book_fill(levels, side=side, requested=left, costs=self.costs)
        if not priced.quantity:
            return {"status": "UNFILLED"}
        if side == "BUY":
            valuation = self._valuation(state, at)
            if valuation["equity"] is None:
                raise ValueError("uncertain portfolio mark blocks entry")
            equity, exposure = valuation["equity"], valuation["position_values"]
            if code not in state["positions"] and len(state["positions"]) >= 3:
                raise ValueError("three-position limit")
            position_budget = equity * Decimal("0.20") - exposure.get(code, Decimal(0))
            total_budget = equity * Decimal("0.60") - sum(exposure.values(), Decimal(0))
            cash = Decimal(state["cash"])
            budget = min(position_budget, total_budget, cash)
            if priced.gross + priced.fee > budget and self._strategy_buy_intent(intent_id):
                priced = _price_book_fill(levels, side=side, requested=left, costs=self.costs, buy_budget=budget)
                if not priced.quantity:
                    return {"status": "UNFILLED_RISK_BUDGET"}
            if priced.gross + priced.fee > position_budget:
                raise ValueError("twenty-percent position limit")
            if priced.gross + priced.fee > total_budget:
                raise ValueError("sixty-percent total limit")
            if priced.gross + priced.fee > cash:
                raise ValueError("cash limit")
        filled, gross, fee, tax = priced.quantity, priced.gross, priced.fee, priced.tax
        state["cash"] = str(Decimal(state["cash"]) + (gross - fee - tax if side == "SELL" else -gross - fee))
        state["positions"][code] = state["positions"].get(code, 0) + (filled if side == "BUY" else -filled)
        if state["positions"][code] == 0:
            del state["positions"][code]
        intent["filled"] += filled
        state["used_quotes"].append(quote_id)
        fill = {"event_id": event_id, "intent_id": intent_id, "symbol": code, "side": side,
                "quantity": filled, "gross": gross, "fee": fee, "tax": tax, "at": at,
                "quote_event_id": quote_id, "cost_version": self.costs.version, "fixture_only": True}
        self.connection.execute("INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, intent_id, code, side, filled, str(gross), str(fee), str(tax), canonical(fill)))
        return {"status": "FILLED" if intent["filled"] == intent["quantity"] else "PARTIAL", "fill": fill}
