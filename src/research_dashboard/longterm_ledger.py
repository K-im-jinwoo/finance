"""Single-account, transactional paper ledger fed by saved UR and official KRX closes."""
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3

from stock_assistant.identifiers import is_korean_security_symbol
from stock_assistant.research_data import APPLICATION_ID as RESEARCH_DB_ID

from .longterm_rules import CONTRACT, LongTermPolicy, buy_candidates, decimal_or_none, exit_reason
from .phase_b_db import UNIFIED_ID, instant, readonly_database, research_database_path, stock_database_path, valid_id


KST = timezone(timedelta(hours=9))
POLICY = LongTermPolicy()
LEDGER_DB_ID = 0x4C545031


def _utc(now):
    return instant(now.isoformat()).astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)


def _day(timestamp):
    return timestamp.astimezone(KST).date().isoformat()


def _regular_close(day):
    return datetime.combine(date.fromisoformat(day), time(15, 30), KST)


def _ratio(value, denominator):
    return str((value / denominator).quantize(Decimal("0.000001"))) if denominator else None


def _order_key(order):
    return (order["side"] != "SELL", order["rank"], order["signal_date"], order["id"])


class LongTermAccounts:
    def __init__(self, root, *, synthetic=False):
        self.root = Path(root)
        self.path = self.root / (os.getenv("LONGTERM_LEDGER_PATH", "").strip() or ".runtime/longterm-paper.sqlite3")
        self.stock_path = stock_database_path(self.root)
        self.research_path = research_database_path(self.root)
        self.price_source = "SYNTHETIC_FIXTURE" if synthetic else "KRX_OPEN_API"

    @contextmanager
    def _write(self, *, create=False):
        for source in (self.stock_path, self.research_path):
            if self.path.resolve() == source.resolve() or (
                self.path.exists() and source.exists() and self.path.samefile(source)
            ):
                raise ValueError("long-term ledger must differ from input databases")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise ValueError("long-term account is unavailable")
        uri = self.path.absolute().as_uri() + ("?mode=rwc" if create else "?mode=rw")
        with closing(sqlite3.connect(uri, uri=True, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            application_id = db.execute("PRAGMA application_id").fetchone()[0]
            if application_id != LEDGER_DB_ID:
                if application_id or db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    raise ValueError("invalid long-term ledger")
                db.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY CHECK(id=1), state_json TEXT NOT NULL)")
                db.execute(f"PRAGMA application_id={LEDGER_DB_ID}")
            yield db

    @staticmethod
    def _load(db):
        row = db.execute("SELECT state_json FROM ledger WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, state):
        db.execute("INSERT INTO ledger VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json",
                   (json.dumps(state, ensure_ascii=False),))

    @contextmanager
    def _research(self):
        with readonly_database(self.research_path) as db:
            if db.execute("PRAGMA application_id").fetchone()[0] != RESEARCH_DB_ID:
                raise ValueError("invalid research database")
            yield db

    @staticmethod
    def _reports(db, now, *, start=None):
        query = """
            SELECT rowid, observed_at, payload_json FROM research_inputs
            WHERE kind='unified_report' AND symbol='ALL' AND json_extract(payload_json,'$.contract')=?
              AND julianday(observed_at)<=julianday(?)
        """
        args = [CONTRACT, now.isoformat()]
        if start is not None:
            query += " AND julianday(observed_at)>=julianday(?)"
            args.append(start)
        rows = []
        if start is None:
            query += " ORDER BY julianday(observed_at) DESC, rowid DESC"
        for key, observed, payload in db.execute(query, args):
            report = json.loads(payload)
            if not isinstance(report, dict) or not valid_id(UNIFIED_ID, report.get("report_id")):
                continue
            try:
                observed_at = instant(observed)
                as_of = instant(report["as_of"])
                expected = report.get("expected_price_session")
                signal_date = max(_day(as_of), date.fromisoformat(expected).isoformat()) if expected else _day(as_of)
            except (ValueError, TypeError, KeyError):
                continue
            if as_of <= observed_at <= now:
                rows.append((observed_at, key, signal_date, report))
                if start is None:
                    return rows
        return sorted(rows, key=lambda item: item[:2])

    def initialize(self, *, now=None):
        now = _utc(now)
        with self._write(create=True) as db:
            state = self._load(db)
            if state is None:
                with self._research() as research:
                    reports = self._reports(research, now)
                state = {"created_at": now.isoformat(), "updated_at": now.isoformat(),
                         "controls": [[now.isoformat(), "RUNNING"]], "initial_row": reports[-1][1] if reports else 0,
                         "cash": str(POLICY.initial_cash), "realized_pnl": "0", "positions": {},
                         "orders": [], "fills": [], "equity": {}, "reports": {}, "last_fill_date": None}
                if reports:
                    observed, _, signal_date, report = reports[-1]
                    self._apply_report(state, report, signal_date, observed, queued_at=now)
                self._save(db, state)
            return self._view(state)

    def control(self, action, *, now=None):
        status = {"pause": "PAUSED", "resume": "RUNNING"}.get(action)
        if status is None:
            raise ValueError("invalid long-term action")
        now = _utc(now)
        with self._write() as db:
            state = self._load(db)
            if state is None or now < instant(state["updated_at"]):
                raise ValueError("account is unavailable at that time")
            if status != state["controls"][-1][1]:
                state["controls"].append([now.isoformat(), status])
                state["updated_at"] = now.isoformat()
                self._save(db, state)
            return self._view(state)

    @staticmethod
    def _status_at(state, at):
        return next((status for timestamp, status in reversed(state["controls"]) if instant(timestamp) <= at), "PAUSED")

    @staticmethod
    def _queue(state, row, side, report_id, signal_date, queued_at, reason, rank=0):
        state["orders"].append({
            "id": len(state["orders"]) + 1, "symbol": row["symbol"], "name": row.get("name") or row["symbol"],
            "side": side, "report_id": report_id, "signal_date": signal_date, "queued_at": queued_at.isoformat(),
            "rank": rank, "status": "PENDING", "reason": reason, "signal_reason": reason,
        })

    def _apply_report(self, state, report, signal_date, observed, *, queued_at=None):
        report_id = report["report_id"]
        positions = state["positions"]
        pending = {(order["side"], order["symbol"]): order for order in state["orders"]
                   if order["status"] in {"PENDING", "DEFER"}}
        candidates = buy_candidates(report, POLICY.max_positions)
        evaluations = {row["symbol"]: row for row in report.get("evaluations", [])
                       if isinstance(row, dict) and is_korean_security_symbol(row.get("symbol"))}
        for candidate in candidates:
            evaluations.setdefault(candidate["symbol"], candidate)
        queued_at = queued_at or observed
        risks = {symbol: exit_reason(row) for symbol, row in evaluations.items()}
        actions = []
        for symbol in sorted(positions.keys() | {symbol for (side, symbol) in pending if side == "BUY"}):
            evaluation = evaluations.get(symbol, {})
            decision = evaluation.get("action_decision")
            reason = risks.get(symbol)
            position = positions.get(symbol)
            review = "REVIEW_REQUIRED" if decision in {None, "VERIFY_PENDING"} else "HOLD_NO_EXIT_TRIGGER"
            if reason:
                if position is not None and ("SELL", symbol) not in pending:
                    self._queue(state, position, "SELL", report_id, signal_date, queued_at, reason)
                    actions.append({"symbol": symbol, "action": "SELL_QUEUED", "reason": reason})
                if ("BUY", symbol) in pending:
                    pending["BUY", symbol].update(status="CANCELLED", reason="CANCELLED_BY_RISK_OR_EXCLUSION",
                                                   cancellation_report_id=report_id)
                    actions.append({"symbol": symbol, "action": "BUY_CANCELLED", "reason": reason})
            latest = {"latest_report_id": report_id if decision is not None else None, "decision": decision,
                      "review_status": reason or review}
            if entry := pending.get(("BUY", symbol)):
                entry["latest_evaluation"] = latest
            if position is not None:
                position.update(latest)
                if not reason and decision != "BUY_RECOMMENDATION":
                    actions.append({"symbol": symbol, "action": review})
        recommendation = None
        for index, row in enumerate(candidates, 1):
            rank = row.get("rank")
            if type(rank) is not int or rank < 1:
                rank = index
            symbol = row["symbol"]
            recommendation = recommendation or {"symbol": symbol, "name": row.get("name") or symbol, "rank": rank}
            if risks[symbol]:
                action = "RISK_BLOCKED"
            elif symbol in positions:
                existing_exit = pending.get(("SELL", symbol))
                positions[symbol]["review_status"] = existing_exit["signal_reason"] if existing_exit else "MAINTAIN"
                action = "MAINTAIN"
            elif ("BUY", symbol) in pending:
                action = "PENDING_MAINTAIN"
            elif self._status_at(state, queued_at) == "PAUSED":
                action = "PAUSED"
            else:
                self._queue(state, row, "BUY", report_id, signal_date, queued_at, "BUY_RECOMMENDATION", rank)
                action = "BUY_QUEUED"
            actions.append({"symbol": symbol, "action": action})
        state["reports"][report_id] = {"report_id": report_id, "as_of": report["as_of"],
                                       "observed_at": observed.isoformat(), "signal_date": signal_date,
                                       "mode": report.get("mode"), "recommendation": recommendation, "actions": actions}

    def _sessions(self, research, stock, start, now):
        through = _day(now)
        sessions = {}
        for (payload,) in research.execute("""
            SELECT payload_json FROM research_inputs WHERE kind='market_calendar' AND symbol='KR'
              AND julianday(observed_at)<=julianday(?) ORDER BY julianday(observed_at), rowid
        """, (now.isoformat(),)):
            payload = json.loads(payload)
            if payload.get("source") != "TOSS_SECURITIES_OPEN_API":
                continue
            for field in ("previousBusinessDay", "today"):
                session = payload.get("calendar", {}).get(field) or {}
                regular = session.get("integrated", {}).get("regularMarket")
                day = session.get("date")
                if not regular or not day or not start <= day <= through:
                    continue
                if isinstance(regular, dict) and regular.get("endTime"):
                    sessions[day] = instant(regular["endTime"])
                else:
                    sessions.setdefault(day, _regular_close(day))
        for (day,) in stock.execute("""
            SELECT DISTINCT trade_date FROM ohlcv WHERE trade_date BETWEEN ? AND ?
              AND julianday(observed_at)<=julianday(?) AND json_extract(payload_json,'$.source')=?
        """, (start, through, now.isoformat(), self.price_source)):
            sessions.setdefault(day, _regular_close(day))
        return {day: close for day, close in sessions.items() if close <= now}

    def _closes(self, stock, now, sessions, symbols):
        closes = {}
        start, end = min(sessions), max(sessions)
        for symbol in symbols:
            for day, observed, payload in stock.execute("""
                SELECT trade_date, observed_at, payload_json FROM ohlcv
                WHERE symbol=? AND trade_date BETWEEN ? AND ? AND julianday(observed_at)<=julianday(?)
                  AND json_extract(payload_json,'$.source')=?
                ORDER BY julianday(observed_at) DESC, observed_at DESC
            """, (symbol, start, end, now.isoformat(), self.price_source)):
                if day not in sessions or (day, symbol) in closes:
                    continue
                try:
                    timestamp = instant(observed)
                except ValueError:
                    continue
                if not sessions[day] <= timestamp <= now:
                    continue
                bar = json.loads(payload)
                price, volume = decimal_or_none(bar.get("close")), decimal_or_none(bar.get("volume"))
                closes[day, symbol] = price if price is not None and price > 0 and volume is not None and volume > 0 else None
        return closes

    @staticmethod
    def _assets(state, day, closes):
        prices = [closes.get((day, symbol)) for symbol in state["positions"]]
        if None in prices:
            return None
        return Decimal(state["cash"]) + sum(price * position["quantity"]
                                            for price, position in zip(prices, state["positions"].values()))

    def _settle(self, state, day, close_at, closes):
        orders = sorted((order for order in state["orders"] if order["status"] in {"PENDING", "DEFER"}
                         and order["signal_date"] < day and instant(order["queued_at"]) <= close_at), key=_order_key)
        for order in orders:
            symbol = order["symbol"]
            side = order["side"]
            price = closes.get((day, symbol))
            if side == "BUY" and self._status_at(state, close_at) == "PAUSED":
                order.update(status="DEFER", reason="ACCOUNT_PAUSED")
            elif price is None:
                order.update(status="PENDING", reason="PENDING_UNTRADABLE_CLOSE")
            elif side == "SELL":
                self._fill(state, order, day, close_at, price, state["positions"][symbol]["quantity"])
            else:
                nav = self._assets(state, day, closes)
                reason = "POSITION_LIMIT" if len(state["positions"]) >= POLICY.max_positions else None
                if reason is None and nav is None:
                    reason = "MISSING_POSITION_CLOSE"
                if reason is None:
                    quantity = POLICY.quantity(nav, price)
                    amount = price * quantity
                    if quantity == 0 or amount + POLICY.fee(amount) > Decimal(state["cash"]):
                        reason = "INSUFFICIENT_CASH"
                if reason:
                    order.update(status="DEFER", reason=reason)
                else:
                    self._fill(state, order, day, close_at, price, quantity)
        self._mark(state, day, close_at, closes)

    @staticmethod
    def _fill(state, order, day, close_at, price, quantity):
        cash = Decimal(state["cash"])
        symbol = order["symbol"]
        amount = price * quantity
        fee = POLICY.fee(amount)
        realized = None
        if order["side"] == "BUY":
            cost = amount + fee
            state["cash"] = str(cash - cost)
            state["positions"][symbol] = {
                "symbol": symbol, "name": order["name"], "quantity": quantity, "entry_date": day,
                "entry_price": str(price), "cost_basis": str(cost), "entry_report_id": order["report_id"],
                "latest_report_id": order["report_id"], "decision": "BUY_RECOMMENDATION", "review_status": "MAINTAIN",
                **order.get("latest_evaluation", {}),
            }
        else:
            position = state["positions"].pop(symbol)
            net = amount - fee
            realized = net - Decimal(position["cost_basis"])
            state["cash"] = str(cash + net)
            state["realized_pnl"] = str(Decimal(state["realized_pnl"]) + realized)
        order.update(status="FILLED", reason=order["signal_reason"])
        state["fills"].append({**{field: order[field] for field in ("id", "side", "symbol", "name", "report_id")},
                               "trade_date": day, "closed_at": close_at.isoformat(), "price": str(price), "quantity": quantity, "fee": str(fee),
                               "realized_pnl": str(realized) if realized is not None else None})
        state["last_fill_date"] = day

    def _mark(self, state, day, close_at, closes):
        cash = Decimal(state["cash"])
        nav = self._assets(state, day, closes)
        peak = POLICY.initial_cash
        mdd = Decimal(0)
        for earlier, point in state["equity"].items():
            if earlier < day and point["total_assets"] is not None:
                peak = max(peak, Decimal(point["total_assets"]))
                mdd = min(mdd, Decimal(point["max_drawdown"]))
        drawdown = None if nav is None else _ratio(nav - max(peak, nav), max(peak, nav))
        cost = sum(Decimal(position["cost_basis"]) for position in state["positions"].values())
        state["equity"][day] = {
            "date": day, "closed_at": close_at.isoformat(), "cash": str(cash),
            "total_assets": str(nav) if nav is not None else None,
            "return": _ratio(nav - POLICY.initial_cash, POLICY.initial_cash) if nav is not None else None,
            "unrealized_pnl": str(nav - cash - cost) if nav is not None else None,
            "realized_pnl": state["realized_pnl"], "drawdown": drawdown,
            "max_drawdown": str(min(mdd, Decimal(drawdown))) if drawdown is not None else str(mdd),
        }
        for symbol, position in state["positions"].items():
            price = closes.get((day, symbol))
            position["mark_date"] = day
            position["close_missing"] = price is None
            if price is not None:
                position.update(last_close=str(price), last_close_date=day)
            value = price * position["quantity"] if price is not None else None
            position["market_value"] = str(value) if value is not None else None
            position["weight"] = _ratio(value, nav) if value is not None and nav is not None else None
            basis = Decimal(position["cost_basis"])
            position["unrealized_pnl"] = str(value - basis) if value is not None else None
            position["return"] = _ratio(value - basis, basis) if value is not None else None

    def refresh(self, *, now=None):
        now = _utc(now)
        with self._write() as db:
            state = self._load(db)
            if state is None or now < instant(state["updated_at"]):
                raise ValueError("account is unavailable at that time")
            created = instant(state["created_at"])
            with self._research() as research, readonly_database(self.stock_path) as stock:
                reports = [item for item in self._reports(research, now, start=state["created_at"])
                           if (item[0] > created or item[0] == created and item[1] > state["initial_row"])
                           and item[-1]["report_id"] not in state["reports"]]
                # Booked fills fix earlier sessions. Retry unsettled closes on or after the last booked session.
                sessions = self._sessions(research, stock, state["last_fill_date"] or _day(created), now)
                sessions = {day: close for day, close in sessions.items() if day > _day(created)}
                symbols = {order["symbol"] for order in state["orders"] if order["status"] in {"PENDING", "DEFER"}}
                symbols.update(state["positions"])
                for _, _, _, report in reports:
                    symbols.update(row["symbol"] for row in buy_candidates(report, POLICY.max_positions))
                closes = self._closes(stock, now, sessions, symbols) if sessions else {}
            events = [(close, 0, day) for day, close in sessions.items()]
            events.extend((observed, key, (signal_date, report)) for observed, key, signal_date, report in reports)
            for at, key, event in sorted(events, key=lambda item: item[:2]):
                if key == 0:
                    self._settle(state, event, at, closes)
                elif event[1]["report_id"] not in state["reports"]:
                    self._apply_report(state, event[1], event[0], at)
            state["updated_at"] = now.isoformat()
            self._save(db, state)
            return self._view(state)

    def account(self):
        try:
            with readonly_database(self.path) as db:
                application_id = db.execute("PRAGMA application_id").fetchone()[0]
                if application_id == LEDGER_DB_ID:
                    state = self._load(db)
                elif application_id == 0 and not db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                    state = None
                else:
                    raise ValueError("invalid long-term ledger")
        except FileNotFoundError:
            state = None
        return self._view(state)

    def overview(self):
        view = self.account()
        overview = {key: value for key, value in view.items()
                    if key not in {"positions", "pending", "orders", "fills", "equity", "reports"}}
        if overview["latest_report"]:
            overview["latest_report"] = {key: value for key, value in overview["latest_report"].items()
                                         if key != "actions"}
        return overview

    @staticmethod
    def _view(state):
        summary = {"status": "NO_ACCOUNT", "policy": POLICY.snapshot(), "updated_at": None, "created_at": None,
                   "latest_report": None, "position_count": 0, "pending_count": 0, "risk_exit_count": 0,
                   "missing_close_count": 0, "metrics": None}
        details = {key: [] for key in ("positions", "pending", "orders", "fills", "equity", "reports")}
        if state is None:
            return {**summary, **details}
        positions = list(state["positions"].values())
        orders = state["orders"]
        pending = sorted((order for order in orders if order["status"] in {"PENDING", "DEFER"}), key=_order_key)
        equity = [state["equity"][day] for day in sorted(state["equity"])]
        reports = list(reversed(state["reports"].values()))
        metrics = equity[-1].copy() if equity else {"date": None, "total_assets": state["cash"], "cash": state["cash"],
                                                    "return": "0", "unrealized_pnl": "0", "realized_pnl": "0",
                                                    "max_drawdown": "0"}
        assets = decimal_or_none(metrics["total_assets"])
        metrics["cash_weight"] = _ratio(Decimal(metrics["cash"]), assets) if assets else None
        return {**summary, "status": state["controls"][-1][1], "updated_at": state["updated_at"],
                "created_at": state["created_at"], "latest_report": reports[0] if reports else None,
                "position_count": len(positions), "pending_count": len(pending),
                "risk_exit_count": sum(order["side"] == "SELL" for order in pending),
                "missing_close_count": sum(position.get("close_missing", False) for position in positions),
                "metrics": metrics, "positions": positions, "pending": pending, "orders": orders,
                "fills": state["fills"], "equity": equity, "reports": reports}
