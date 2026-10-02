from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from .contracts import digest, instant, number, validate_dataset
from .rules import Policy, invalidation


def money(value):
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class Engine:
    """Chronological paper ledger; only explicit verified session OPEN can fill."""
    def __init__(self, dataset: dict, policy: Policy):
        self.dataset = dataset
        self.coverage = validate_dataset(dataset)
        self.policy = policy
        self.s = {
            "status": "READY", "cash": policy.initial_cash, "positions": {}, "pending": {},
            "reports": [], "trades": [], "checks": {}, "invalidated": {}, "observations": [],
            "outbox": [], "equity": [], "processed": {}, "clock": None,
            "pause_at": None, "resume_at": None, "total_cost": "0", "realized_pnl": "0",
            "peak": policy.initial_cash, "max_drawdown": "0", "marks": {}, "closed_positions": [],
        }

    def emit(self, event, kind, symbol=None, detail=None):
        key = digest({"event": detail["trade_date"] if kind == "EVENING_SUMMARY" else event["id"], "kind": kind, "symbol": symbol})[:24]
        if not any(item["event_id"] == key for item in self.s["outbox"]):
            self.s["outbox"].append({"event_id": key, "kind": kind, "symbol": symbol, "at": event["at"], "detail": detail,
                                      "status": "DRAFT_ONLY", "source_event_id": event["id"]})

    def cancel_pending(self, event, reason):
        for symbol, order in self.s["pending"].items():
            self.s["observations"].append({"at": event["at"], "symbol": symbol, "state": "CANCELLED", "reason": reason, "order": deepcopy(order)})
        self.s["pending"] = {}

    def process(self, event):
        fingerprint = digest(event)
        if event["id"] in self.s["processed"]:
            if self.s["processed"][event["id"]] != fingerprint:
                raise ValueError("immutable event id has changed content")
            return
        validate_dataset({**self.dataset, "events": [event]})
        at = instant(event["at"])
        if self.s["clock"] and at < instant(self.s["clock"]):
            raise ValueError("cannot insert past events")
        if self.s["status"] in {"COMPLETED", "FAILED"}:
            raise ValueError("finished experiment is immutable")
        kind = event["type"]
        if kind == "PAUSE":
            self.cancel_pending(event, "USER_PAUSE")
            self.s["status"] = "PAUSED"
            self.s["pause_at"] = event["at"]
        elif kind == "RESUME":
            if self.s["status"] != "PAUSED":
                raise ValueError("only paused experiment can resume")
            self.s["status"] = "RUNNING"
            self.s["resume_at"] = event["at"]
        elif kind == "REPORT":
            self.report(event)
        elif kind == "CHECK":
            self.check(event)
        elif kind == "OPEN":
            self.open(event)
        elif kind == "MARK":
            self.mark(event)
            if event.get("evening_summary") and event.get("verified_trade_date"):
                self.emit(event, "EVENING_SUMMARY", detail={"trade_date": event["verified_trade_date"], "equity": deepcopy(self.s["equity"][-1]), "status": self.s["status"], "total_cost": self.s["total_cost"], "pending": len(self.s["pending"]), "review_required": "DISCLOSURE"})
        elif kind == "ERROR":
            self.emit(event, "ERROR", detail=event.get("reason", "data error"))
            if event.get("fatal"):
                self.cancel_pending(event, "FAILED")
                self.s["status"] = "FAILED"
        elif kind == "END":
            self.cancel_pending(event, "END_OF_PERIOD")
            self.s["status"] = "COMPLETED"
        if self.s["status"] == "READY":
            self.s["status"] = "RUNNING"
        self.s["clock"] = event["at"]
        self.s["processed"][event["id"]] = fingerprint
        self.assert_invariants()

    def reentry_allowed(self, symbol, event):
        invalid = self.s["invalidated"].get(symbol)
        if invalid is None:
            return True
        checks = {c["code"]: c for c in self.s["checks"].get(symbol, [])}
        for cause in invalid["causes"]:
            resolved = checks.get(cause["code"], {})
            if resolved.get("status") != "VALID" or resolved.get("evidence_id") == cause.get("evidence_id"):
                return False
            if instant(resolved["available_at"]) <= instant(invalid["at"]) or instant(resolved["available_at"]) > instant(event["at"]):
                return False
        return True

    def report(self, event):
        self.s["reports"].append(deepcopy(event))
        for rank, candidate in enumerate(event["candidates"]):
            symbol = candidate["symbol"]
            state = "REVIEW"
            eligible = candidate["review_tier"] == "PRIORITY_REVIEW" and candidate["decision"] != "EXCLUDED"
            if symbol in self.s["positions"]:
                state = "MAINTAIN"
                self.s["positions"][symbol]["report_ids"] = list(dict.fromkeys(self.s["positions"][symbol]["report_ids"] + [event["report_id"]]))
            elif eligible and self.s["status"] != "PAUSED":
                if self.s["resume_at"] and instant(event["at"]) <= instant(self.s["resume_at"]):
                    state = "DEFER_OLD_SIGNAL"
                elif not self.reentry_allowed(symbol, event):
                    state = "DEFER_UNRESOLVED_INVALIDATION"
                elif symbol in self.s["pending"]:
                    state = "PENDING_MAINTAIN"
                else:
                    state = "NEW"
                    self.s["pending"][symbol] = {"side": "BUY", "created_at": event["at"], "report_id": event["report_id"], "source_event_id": event["id"], "rank": rank,
                                                   "decision": candidate["decision"], "review_tier": candidate["review_tier"], "reentry": symbol in self.s["invalidated"]}
            self.s["observations"].append({"at": event["at"], "report_id": event["report_id"], "symbol": symbol, "state": state,
                                           "original_decision": candidate["decision"], "original_review_tier": candidate["review_tier"]})

    def check(self, event):
        symbol = event["symbol"]
        results = invalidation(self.dataset["securities"][symbol], event)
        self.s["checks"][symbol] = results
        causes = [item for item in results if item["status"] == "INVALID"]
        if not causes:
            return
        previous = self.s["invalidated"].get(symbol)
        if symbol in self.s["positions"] or (symbol in self.s["pending"] and self.s["pending"][symbol]["side"] == "BUY"):
            # Keep every unresolved cause; a later partial invalidation cannot erase one.
            combined = {item["code"]: item for item in (previous or {}).get("causes", [])}
            combined.update({item["code"]: item for item in causes})
            changed = not previous or digest(previous["causes"]) != digest(list(combined.values()))
            if changed:
                self.s["invalidated"][symbol] = {"at": event["at"], "causes": list(combined.values())}
            if not previous or set(combined) - {c["code"] for c in previous["causes"]}:
                self.emit(event, "INVALIDATION", symbol, results)
            if symbol in self.s["positions"]:
                if self.s["status"] != "PAUSED":
                    self.s["pending"].setdefault(symbol, {"side": "SELL", "created_at": event["at"], "source_event_id": event["id"], "report_id": self.s["positions"][symbol]["report_ids"][0], "causes": causes, "rank": -1})
                    if self.s["pending"][symbol]["side"] != "SELL":
                        raise ValueError("held security cannot have pending buy")
            else:
                self.s["pending"].pop(symbol, None)

    def open(self, event):
        if self.s["status"] == "PAUSED":
            return
        opened = instant(event["open_at"])
        prices = event["prices"]
        # No future close for NAV. A missing open for any holding blocks new allocations.
        baseline = None
        if all(symbol in prices for symbol in self.s["positions"]):
            baseline = number(self.s["cash"]) + sum(number(prices[s]) * p["quantity"] for s, p in self.s["positions"].items())
        ordered = sorted(list(self.s["pending"].items()), key=lambda item: (0 if item[1]["side"] == "SELL" else 1, instant(item[1]["created_at"]), item[1]["source_event_id"], item[1]["rank"]))
        fee_rate = number(self.policy.round_trip_bps) / 20000
        for symbol, order in ordered:
            if instant(order["created_at"]) >= opened:
                continue
            if symbol not in prices or symbol in event.get("suspended", []):
                self.s["observations"].append({"at": event["at"], "symbol": symbol, "state": "PENDING", "reason": "OPEN_UNAVAILABLE_OR_SUSPENDED"})
                continue
            price = number(prices[symbol])
            side = order["side"]
            if side == "BUY":
                if len(self.s["positions"]) >= self.policy.max_positions or baseline is None:
                    self.s["observations"].append({"at": event["at"], "symbol": symbol, "state": "DEFERRED", "reason": "POSITION_LIMIT" if baseline is not None else "NAV_UNAVAILABLE"})
                    self.s["pending"].pop(symbol)
                    continue
                budget = baseline * number(self.policy.entry_fraction)
                quantity = int((budget / (price * (1 + fee_rate))).to_integral_value(rounding=ROUND_DOWN))
                while quantity > 0 and price * quantity + money(price * quantity * fee_rate) > budget:
                    quantity -= 1
                fee = money(price * quantity * fee_rate)
                cost = price * quantity + fee
                if quantity == 0 or cost > number(self.s["cash"]):
                    self.s["observations"].append({"at": event["at"], "symbol": symbol, "state": "DEFERRED", "reason": "CASH_INSUFFICIENT" if quantity else "LOT_TOO_EXPENSIVE"})
                    self.s["pending"].pop(symbol)
                    continue
                self.s["cash"] = str(number(self.s["cash"]) - cost)
                self.s["positions"][symbol] = {"quantity": quantity, "entry_price": str(price), "entry_fee": str(fee), "cost": str(cost), "entry_at": event["open_at"], "report_ids": [order["report_id"]], "original_decision": order["decision"]}
                if order.get("reentry"):
                    self.s["invalidated"].pop(symbol, None)
            else:
                pos = self.s["positions"].pop(symbol)
                quantity = pos["quantity"]
                fee = money(price * quantity * fee_rate)
                proceeds = price * quantity - fee
                pnl = proceeds - number(pos["cost"])
                self.s["cash"] = str(number(self.s["cash"]) + proceeds)
                self.s["realized_pnl"] = str(number(self.s["realized_pnl"]) + pnl)
                self.s["closed_positions"].append({"symbol": symbol, "entry_at": pos["entry_at"], "exit_at": event["open_at"], "pnl": str(pnl), "report_ids": pos["report_ids"]})
            self.s["total_cost"] = str(number(self.s["total_cost"]) + fee)
            trade = {"trade_id": digest({"source": order["source_event_id"], "symbol": symbol, "side": side})[:24], "symbol": symbol, "side": side, "quantity": quantity,
                     "price": str(price), "fee": str(fee), "filled_at": event["open_at"], "recorded_at": event["at"], "signal_at": order["created_at"],
                     "report_id": order["report_id"], "source_event_id": order["source_event_id"], "session_id": event["session_id"]}
            self.s["trades"].append(trade)
            self.s["pending"].pop(symbol)
            self.emit(event, side, symbol, trade)

    def mark(self, event):
        prices = event["prices"]
        missing = [s for s in self.s["positions"] if s not in prices]
        stale = [s for s in self.s["positions"] if s in event.get("stale_symbols", [])]
        unknown_checks = [s for s in self.s["positions"] if s not in self.s["checks"]]
        row = {"at": event["at"], "cash": self.s["cash"], "missing_symbols": missing, "stale_symbols": stale, "unchecked_symbols": unknown_checks,
               "total_assets": None, "return": None, "unrealized_pnl": None}
        self.s["marks"] = deepcopy(prices)
        if not missing and not stale:
            value = sum(number(prices[s]) * p["quantity"] for s, p in self.s["positions"].items())
            assets = number(self.s["cash"]) + value
            unrealized = value - sum(number(p["cost"]) for p in self.s["positions"].values())
            row.update(total_assets=str(assets), return_value=str(assets / number(self.policy.initial_cash) - 1), unrealized_pnl=str(unrealized))
            row["return"] = row.pop("return_value")
            peak = max(number(self.s["peak"]), assets)
            self.s["peak"] = str(peak)
            self.s["max_drawdown"] = str(min(number(self.s["max_drawdown"]), assets / peak - 1))
        self.s["equity"].append(row)

    def assert_invariants(self):
        if number(self.s["cash"]) < 0 or len(self.s["positions"]) > self.policy.max_positions:
            raise AssertionError("cash or position invariant")
        if any(p["quantity"] <= 0 for p in self.s["positions"].values()):
            raise AssertionError("positive integer lots required")
        expected = number(self.policy.initial_cash)
        for trade in self.s["trades"]:
            amount = number(trade["price"]) * trade["quantity"]
            expected += amount - number(trade["fee"]) if trade["side"] == "SELL" else -(amount + number(trade["fee"]))
        if expected != number(self.s["cash"]):
            raise AssertionError("cash ledger reconciliation failed")

    def result(self):
        closed = self.s["closed_positions"]
        equity = self.s["equity"][-1] if self.s["equity"] else {}
        periods = [{"symbol": p["symbol"], "entry_at": p["entry_at"], "exit_at": p["exit_at"], "calendar_days": (instant(p["exit_at"]) - instant(p["entry_at"])).days} for p in closed]
        open_periods = [{"symbol": symbol, "entry_at": p["entry_at"], "calendar_days": (instant(self.s["clock"]) - instant(p["entry_at"])).days if self.s["clock"] else None} for symbol,p in self.s["positions"].items()]
        return {"coverage": self.coverage, "policy": self.policy.snapshot(), "state": deepcopy(self.s),
                "metrics": {"completed_round_trips": len(closed), "win_rate": None if not closed else str(Decimal(sum(number(p["pnl"]) > 0 for p in closed)) / len(closed)),
                            "open_positions": len(self.s["positions"]), "pending_trades": len(self.s["pending"]), "total_cost": self.s["total_cost"], "realized_pnl": self.s["realized_pnl"], "max_drawdown": self.s["max_drawdown"] if any(e["total_assets"] is not None for e in self.s["equity"]) else None,
                            "cash_ratio": str(number(equity["cash"])/number(equity["total_assets"])) if equity.get("total_assets") else None,
                            "holding_periods_closed": periods, "holding_periods_open": open_periods,
                            "valuation_missing_count": sum(e["total_assets"] is None for e in self.s["equity"]),
                            "benchmark": None, "benchmark_status": "NOT_SELECTED", "mdd_basis": "verified mark observations only; no intraday MDD claim"}}

    def run(self):
        for event in self.dataset["events"]:
            self.process(event)
        return self.result()

