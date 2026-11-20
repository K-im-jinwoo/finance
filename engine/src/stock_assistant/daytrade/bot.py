"""Offline synthetic session orchestration over the unchanged audited ledger."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN
import json
from pathlib import Path

from . import POLICY_VERSION
from .contracts import KST, canonical, digest, identifier, instant, number
from .ledger import DaytradeLedger, FixtureCosts
from .market import QualityPolicy, normalize
from .report import build_trading_report, trading_inventory
from .risk import RiskState, Phase
from .scanner import ScannerConfig, orderbook_metrics, scan_rankings, risk_reasons, stock_reference_reasons
from .strategy import StrategyConfig, StrategyEngine


@dataclass(frozen=True)
class BotConfig:
    initial_capital: str
    policy_version: str
    seed: int
    quality: QualityPolicy
    fixture_costs: FixtureCosts
    scanner_config: ScannerConfig
    strategy_config: StrategyConfig
    max_daily_entries: int | None = None
    reentry_cooldown_seconds: int | None = None

    def __post_init__(self):
        number(self.initial_capital, positive=True)
        if self.policy_version != POLICY_VERSION or type(self.seed) is not int or self.seed < 0:
            raise ValueError("current policy version and explicit non-negative seed required")
        for value, expected in ((self.quality, QualityPolicy), (self.fixture_costs, FixtureCosts),
                                (self.scanner_config, ScannerConfig), (self.strategy_config, StrategyConfig)):
            if not isinstance(value, expected):
                raise ValueError("explicit typed bot configuration required")
        if self.max_daily_entries is not None and (type(self.max_daily_entries) is not int or self.max_daily_entries < 1):
            raise ValueError('positive daily entry limit required')
        if self.reentry_cooldown_seconds is not None and (type(self.reentry_cooldown_seconds) is not int or self.reentry_cooldown_seconds < 0):
            raise ValueError('nonnegative reentry cooldown required')

    @classmethod
    def from_dataset(cls, dataset):
        if not isinstance(dataset, dict) or dataset.get("dataset_kind") != "SYNTHETIC":
            raise ValueError("bot execution requires an explicit SYNTHETIC dataset")
        return cls(dataset["initial_capital"], dataset["policy_version"], dataset["seed"],
            QualityPolicy(**dataset["quality"]), FixtureCosts(**dataset["fixture_costs"]),
            ScannerConfig(**dataset["scanner_config"]), StrategyConfig(**dataset["strategy_config"]),
            dataset.get('max_daily_entries'), dataset.get('reentry_cooldown_seconds'))


def _events(dataset):
    events = dataset.get("events")
    if events is None and "rankings" in dataset:
        at = dataset["at"]
        events = [{"id": "scan-session", "sequence": 0, "at": at, "kind": "SESSION",
                   "data": {"normal_session_verified": False}},
                  {"id": "scan-rankings", "sequence": 1, "at": at, "kind": "RANKINGS",
                   "data": {"turnover": dataset["rankings"], "volume": dataset.get("volume_rankings")}}]
    if not isinstance(events, list) or not events:
        raise ValueError("nonempty chronological event list required")
    ordered, seen, previous, sequences = [], {}, None, set()
    for event in events:
        identifier(event["id"])
        fingerprint = digest(event)
        if event["id"] in seen:
            if seen[event["id"]] != fingerprint:
                raise ValueError("conflicting bot input identity")
            continue
        seq = event["sequence"]
        if type(seq) is not int or seq < 0 or seq in sequences:
            raise ValueError("unique non-negative input sequence required")
        at = instant(event["at"])
        if previous is not None and (at < previous[0] or seq <= previous[1]):
            raise ValueError("bot input order must advance time and sequence")
        if event.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            raise ValueError("input policy version mismatch")
        if event["kind"] not in {"SESSION", "RANKINGS", "MARKET", "END_SESSION", "CLOCK", "LOCK", "RECOVER"}:
            raise ValueError("external intents/fills are not bot inputs")
        if not isinstance(event.get("data"), dict):
            raise ValueError("event object data required")
        seen[event["id"]], previous = fingerprint, (at, seq)
        sequences.add(seq)
        ordered.append(event)
    return ordered


def _observation(event, quality, *, event_id=None):
    data = event["data"]
    return normalize(data["feed"], data["payload"], event_id=event_id or event["id"],
        received_at=event["at"], received_monotonic=data["received_monotonic"],
        session_id=instant(event["at"]).astimezone(KST).date().isoformat(),
        issuer=data.get("symbol", "ALL"), source=data.get("source"), quality=quality)


def scan_dataset(dataset, *, quotes=None):
    """CLI discovery with no ledger or writes; pre-scan books remain point-in-time."""
    config = ScannerConfig(**dataset["scanner_config"])
    quality = QualityPolicy(**dataset["quality"])
    books = {}
    for event in _events(dataset):
        if event["kind"] == "MARKET" and event["data"]["feed"] == "orderbook":
            observation = _observation(event, quality)
            books[observation.symbol] = observation
        if event["kind"] != "RANKINGS":
            continue
        if quotes is not None:
            books = {code: normalize("orderbook", payload, event_id="scan-quote-" + code,
                received_at=event["at"], received_monotonic=0,
                session_id=instant(event["at"]).astimezone(KST).date().isoformat(), issuer=code, quality=quality)
                for code, payload in quotes.items()}
        data = event["data"]
        return scan_rankings(data["turnover"], data.get("volume"), config=config, now=event["at"],
                             quotes=books, risk_meta=data.get("risk_meta", dataset.get("risk_meta")))
    raise ValueError("rankings event required for scan")


class _PaperSession:
    def __init__(self, ledger, config, dataset_hash, *, compact_live=False):
        self.ledger, self.config = ledger, config
        self.strategy = StrategyEngine(config.strategy_config)
        self.sequence = 0
        self.watches, self.books = {}, {}
        self.inactive = set()
        self.subscription_symbols=None  # None retains the offline replay contract.
        self.critical_sequence=0
        self.ranking_source_issues=()
        self.compact_live, self.last_signal = compact_live, {}
        self.meta = {"profile": config.strategy_config.profile, "dataset_hash": dataset_hash,
                     "scan_history": [], "signals": [], "decisions": [], "equity_curve": []}

    def emit(self, kind, data, at):
        self.sequence += 1
        event_id = f"bot-{self.sequence:08d}"
        result = self.ledger.apply({"id": event_id, "sequence": self.sequence, "at": at,
            "kind": kind, "policy_version": self.config.policy_version, "data": data})
        if kind in {'INTENT','FILL'}:
            self.critical_sequence=self.sequence
        state = self.ledger.state()
        equity = self.ledger.valuation(at)["equity"]
        point = {"at": instant(at).isoformat(), "equity": equity,
            "phase": state["risk"]["phase"], "locks": sorted(state["risk"]["reasons"])}
        curve = self.meta["equity_curve"]
        if self.compact_live and curve and all(curve[-1][key] == point[key] for key in ("equity", "phase", "locks")):
            curve[-1] = point
        else:
            curve.append(point)
        return event_id, result

    def decision(self, at, code, status, reason, **details):
        self.meta["decisions"].append({"at": instant(at).isoformat(), "symbol": code,
                                       "status": status, "reason": reason, **details})

    def pending(self, *, code=None, side=None):
        return {key: intent for key, intent in self.ledger.state()["intents"].items()
                if key not in self.inactive and intent["filled"] < intent["quantity"]
                and (code is None or intent["symbol"] == code) and (side is None or intent["side"] == side)}

    def fill_pending(self, book, at):
        for key, intent in self.pending(code=book.symbol).items():
            risk = RiskState.restore(self.ledger.state()["risk"])
            if intent["side"] == "BUY" and not risk.candidate_entry_allowed:
                self.inactive.add(key)
                self.decision(at, book.symbol, "SKIPPED", "BUY_REMAINDER_ENTRY_BLOCKED", intent_id=key)
                continue
            if intent["side"] == "BUY":
                blocked=self.entry_block_reason(book.symbol,at,reserved_intent=key)
                if blocked:
                    self.inactive.add(key)
                    self.decision(at,book.symbol,'SKIPPED',blocked,intent_id=key)
                    continue
                candidate = self.watches.get(book.symbol)
                if (candidate is None or candidate["entry_blocked"] or candidate["valid_until"] is None
                        or instant(at) > instant(candidate["valid_until"])):
                    self.inactive.add(key)
                    self.decision(at, book.symbol, "SKIPPED", "BUY_REMAINDER_CANDIDATE_UNAVAILABLE", intent_id=key)
                    continue
                metrics = orderbook_metrics(book, now=at,
                    max_age_seconds=self.config.strategy_config.quote_max_age_seconds, issuer=book.symbol)
                if metrics["issues"] or metrics["spread_bps"] > self.config.strategy_config.max_spread_bps:
                    self.decision(at, book.symbol, "SKIPPED", "BUY_FILL_QUOTE_QUALITY", intent_id=key)
                    continue
                minimum=self.config.strategy_config.min_bid_notional
                if minimum is not None and metrics['bid']*metrics['bid_volume'] < minimum:
                    self.decision(at,book.symbol,'SKIPPED','BUY_FILL_QUOTE_DEPTH',intent_id=key)
                    continue
                max_ratio=self.config.strategy_config.max_ask_bid_volume_ratio
                if max_ratio is not None:
                    bid_total=sum((number(v['volume']) for v in book.data['bids']),Decimal(0))
                    ask_total=sum((number(v['volume']) for v in book.data['asks']),Decimal(0))
                    if not bid_total or ask_total/bid_total > max_ratio:
                        self.decision(at,book.symbol,'SKIPPED','BUY_FILL_QUOTE_DEPTH',intent_id=key)
                        continue
            earliest = instant(intent["at"]) + timedelta(milliseconds=self.config.fixture_costs.latency_ms)
            if (book.issues or book.source_timestamp is None or instant(book.source_timestamp) < earliest
                    or instant(book.received_at_utc) < earliest):
                self.decision(at, book.symbol, "SKIPPED", "POST_LATENCY_VALID_BOOK_REQUIRED", intent_id=key)
                continue
            # Exactly one attempt per new book. Ledger validates latest quote,
            # depth, participation, cash and exposure atomically.
            try:
                _, result = self.emit("FILL", {"intent_id": key, "quote_event_id": book.event_id}, at)
            except ValueError as exc:
                self.decision(at, book.symbol, "REJECTED", "LEDGER_FILL_CONSTRAINT", intent_id=key,
                              constraint=str(exc))
                continue
            self.decision(at, book.symbol, result["status"], "OBSERVED_POST_LATENCY_BOOK", intent_id=key)

    def buy_quantity(self, code, at):
        state = self.ledger.state()
        valuation = self.ledger.valuation(at)
        if valuation["equity"] is None or code not in self.books:
            return 0
        costs = self.config.fixture_costs
        pending = self.pending(side="BUY")
        names = set(state["positions"]) | {i["symbol"] for i in pending.values()}
        if code not in names and len(names) >= 3:
            return 0
        reserve = Decimal(0)
        for intent in pending.values():
            book = self.books.get(intent["symbol"])
            if book is None or not book.data["asks"]:
                return 0
            unit = Decimal(book.data["asks"][0]["price"]) * (1 + Decimal(costs.extra_slippage_bps) / 10000)
            gross = unit * (intent["quantity"] - intent["filled"])
            reserve += gross + costs.charge(gross, costs.buy_fee_bps)
        equity = valuation["equity"]
        exposure = valuation["position_values"]
        budget = min(equity * Decimal("0.20") - exposure.get(code, Decimal(0)),
                     equity * Decimal("0.60") - sum(exposure.values(), Decimal(0)) - reserve,
                     Decimal(state["cash"]) - reserve)
        if budget <= 0:
            return 0
        unit = Decimal(self.books[code].data["asks"][0]["price"]) * (1 + Decimal(costs.extra_slippage_bps) / 10000)
        quantity = int((budget / (unit * (1 + Decimal(costs.buy_fee_bps) / 10000))).to_integral_value(rounding=ROUND_DOWN))
        while quantity > 0 and unit * quantity + costs.charge(unit * quantity, costs.buy_fee_bps) > budget:
            quantity -= 1
        return quantity

    def entry_history(self, at):
        """Reconstruct entry reservations and full exits from committed paper data."""
        day=instant(at).astimezone(KST).date()
        filled,quantities,entered,last_flat=set(),{},{},{}
        for raw, in self.ledger.connection.execute('SELECT f.payload FROM paper_fills f JOIN events e ON e.id=f.event_id ORDER BY e.sequence'):
            fill=json.loads(raw)
            if instant(fill['at'])>instant(at):
                continue
            code,qty=fill['symbol'],fill['quantity']
            previous=quantities.get(code,0)
            if fill['side']=='BUY':
                if instant(fill['at']).astimezone(KST).date()==day:
                    filled.add(fill['intent_id'])
                if previous==0:
                    entered[code]=fill['at']
                quantities[code]=previous+qty
            else:
                quantities[code]=previous-qty
                if quantities[code]==0:
                    last_flat[code]=fill['at']
                    entered.pop(code,None)
        reserved={key for key,value in self.pending(side='BUY').items()
            if instant(value['at']).astimezone(KST).date()==day and instant(value['at'])<=instant(at)}
        return {'filled_entries':len(filled),'filled_intents':sorted(filled),
            'reserved_intents':sorted(reserved-filled),'used_entries':len(filled|reserved),
            'entered_at':entered,'last_flat_at':last_flat}

    def entry_block_reason(self, code, at, *, reserved_intent=None):
        issues=self.watches.get(code,{}).get('issues',())
        if issues:
            return 'STALE_RANKINGS' if 'STALE_RANKINGS' in issues else 'RANKINGS_UNAVAILABLE'
        if self.subscription_symbols is not None and code not in self.subscription_symbols:
            return 'NOT_SUBSCRIBED'
        history=self.entry_history(at)
        last=history['last_flat_at'].get(code)
        if self.config.reentry_cooldown_seconds and last is not None and (
                instant(at)-instant(last)).total_seconds() < self.config.reentry_cooldown_seconds:
            return 'REENTRY_COOLDOWN'
        limit=self.config.max_daily_entries
        if limit is not None:
            used=history['used_entries']
            owns=reserved_intent in history['filled_intents']+history['reserved_intents']
            if used > limit or used==limit and not owns:
                return 'DAILY_ENTRY_LIMIT'
        return None

    def evaluate(self, at):
        # Strategy positions require fills only; avoid copying the entire event
        # history into memory on every tick in a long-running live session.
        fills = [json.loads(row[0]) for row in self.ledger.connection.execute("SELECT payload FROM paper_fills ORDER BY event_id")]
        inventory, _, _ = trading_inventory(fills)
        positions = {code: {"quantity": item["quantity"], "average_price": item["cost"] / item["quantity"]}
                     for code, item in inventory.items() if item["quantity"]}
        history=self.entry_history(at)
        for code,position in positions.items():
            position['entry_at']=history['entered_at'].get(code)
        if self.config.max_daily_entries is not None or self.config.reentry_cooldown_seconds is not None:
            self.meta['entry_controls']={**history,'max_daily_entries':self.config.max_daily_entries,
                'reentry_cooldown_seconds':self.config.reentry_cooldown_seconds}
        for code in [*sorted(positions), *sorted(set(self.watches) - set(positions))]:
            # Risk can change after each generated intent/fill; never cache it.
            risk = RiskState.restore(self.ledger.state()["risk"])
            signal = self.strategy.evaluate(code, at, risk, positions, self.watches.get(code))
            record = {"at": instant(at).isoformat(), "symbol": code, **asdict(signal)}
            prior = self.last_signal.get(code)
            history = self.meta["signals"]
            same_hold = (self.compact_live and signal.action == "HOLD" and prior is not None
                and all(history[prior].get(key) == record.get(key) for key in ("action", "exit_reason", "reasons")))
            if same_hold:
                history[prior] = record
            else:
                self.last_signal[code] = len(history)
                history.append(record)
            if signal.action == "HOLD":
                continue
            if risk.phase == Phase.CLOSED:
                self.decision(at, code, "SKIPPED", "SESSION_CLOSED")
                continue
            if signal.action == "SELL":
                for key in self.pending(code=code, side="BUY"):
                    self.inactive.add(key)
                    self.decision(at, code, "SKIPPED", "BUY_REMAINDER_CANCELLED_FOR_EXIT", intent_id=key)
                if self.pending(code=code, side="SELL"):
                    continue
                quantity = positions[code]["quantity"]
            else:
                if self.pending(code=code, side="BUY") or self.pending(code=code, side="SELL"):
                    continue
                blocked=self.entry_block_reason(code,at)
                if blocked:
                    self.decision(at,code,'SKIPPED',blocked)
                    continue
                quantity = self.buy_quantity(code, at) if risk.candidate_entry_allowed else 0
                if quantity < 1:
                    self.decision(at, code, "SKIPPED", "CAPITAL_OR_POSITION_LIMIT")
                    continue
            _, result = self.emit("INTENT", {"symbol": code, "side": signal.action,
                "quantity": quantity, "strategy_signal": asdict(signal)}, at)
            self.decision(at, code, "INTENT", signal.exit_reason or "VWAP_BREAKOUT", quantity=quantity)
        self.trim_live_history()

    def trim_live_history(self, *, signal_limit=200, scan_limit=20):
        """Bound replaceable live diagnostics; events/fills and offline history stay intact."""
        if not self.compact_live:
            return
        scans=self.meta['scan_history'][-scan_limit:]
        for scan in scans:
            rejected=scan.get('rejected',[])
            if len(rejected)>5:
                scan['rejected_count']=len(rejected)
                scan['rejected_counts']=dict(Counter(r['reason'] for r in rejected))
                scan['rejected']=rejected[:5]
        self.meta['scan_history']=scans
        signals=self.meta['signals']
        latest={row['symbol']:i for i,row in enumerate(signals)}
        indexes=list(latest.values())
        if len(indexes)>200:
            indexes=sorted(indexes,key=lambda i:(instant(signals[i]['at']),i))[-200:]
        chosen=set(indexes)
        signal_limit=max(signal_limit,len(chosen))
        for i in range(len(signals)-1,-1,-1):
            if len(chosen)>=signal_limit:break
            chosen.add(i)
        self.meta['signals']=[signals[i] for i in sorted(chosen)]
        self.last_signal={row['symbol']:i for i,row in enumerate(self.meta['signals'])}
        self.meta['decisions']=self.meta['decisions'][-100:]
        curve=self.meta['equity_curve']
        if len(curve)>64:
            tail=curve[-63:]
            # Preserve honest drawdown uncertainty after diagnostic history truncation.
            marker={**tail[0],'equity':None,'diagnostic_truncation':True}
            curve=[marker]+tail
        self.meta['equity_curve']=curve
        self.meta['diagnostic_retention']={'scan_history':scan_limit,'signals':signal_limit,
            'rejected_per_scan':5,'decisions':100,'equity_curve':64,'live_only':True}

    def refresh_watch_quote(self, book, at):
        candidate = self.watches.get(book.symbol)
        if candidate is None:
            return
        metrics = orderbook_metrics(book, now=at,
            max_age_seconds=self.config.scanner_config.quote_max_age_seconds, issuer=book.symbol)
        eligible = not metrics["issues"] and metrics["spread_bps"] <= self.config.scanner_config.max_spread_bps and not candidate.get('issues')
        candidate.update(spread_bps=metrics["spread_bps"] if not metrics["issues"] else None,
                         entry_blocked=not eligible)
        reasons = [reason for reason in candidate["selection_reasons"]
                   if reason not in {"QUOTE_UNAVAILABLE_WATCH_ONLY", "QUOTE_ELIGIBLE"}]
        candidate["selection_reasons"] = reasons + ["QUOTE_ELIGIBLE" if eligible else "QUOTE_UNAVAILABLE_WATCH_ONLY"]
        for row in self.meta["scan_history"][-1]["selected"]:
            if row["symbol"] == book.symbol:
                row.update(candidate)
                break

    def recover_retired_feeds(self, at):
        # An empty pool is not evidence that every issuer is safe to forget.
        if not self.watches:
            return
        state = self.ledger.state()
        for reason in list(state["risk"]["reasons"].get("DATA_DEGRADED", [])):
            feed, separator, code = reason.partition(":")
            if (separator and feed in {"orderbook", "trade", "candles"}
                    and code not in self.watches and code not in state["positions"]):
                self.emit("RECOVER", {"lock":"DATA_DEGRADED", "reason":reason, "verified":True}, at)

    def step(self, event, risk_meta=None):
        """Advance one validated event using the same execution path as replay."""
        at, data, kind = event["at"], event["data"], event["kind"]
        if kind == "RANKINGS":
            self.emit("CLOCK", {"input_id": event["id"]}, at)
            # Expired cached quotes must not prevent subscribing for a new one.
            # Supplied fresh-but-invalid books still go through strict screening.
            active_quotes = {code: book for code, book in self.books.items()
                if "STALE_ORDERBOOK" not in orderbook_metrics(book, now=at,
                    max_age_seconds=self.config.scanner_config.quote_max_age_seconds, issuer=code)["issues"]}
            scan = scan_rankings(data["turnover"], data.get("volume"),
                config=self.config.scanner_config, now=at, quotes=active_quotes,
                risk_meta=data.get("risk_meta", risk_meta))
            self.ranking_source_issues=scan.source_issues
            previous=self.watches
            self.watches = {r["symbol"]: r for r in scan.selected}
            if self.compact_live and not scan.selected and scan.source_issues:
                # A stale source is not evidence that an otherwise valid watch
                # should lose its subscription and accumulated warm-up coverage.
                extra={r['symbol']:set(r['reasons'])-set(scan.source_issues) for r in scan.rejected}
                metadata=data.get('risk_meta',risk_meta) or {}
                for code in previous:
                    adverse=list(risk_reasons(metadata.get(code)))
                    if self.config.scanner_config.require_common_stock:
                        adverse.extend(stock_reference_reasons(metadata.get(code),now=at))
                    if adverse:extra.setdefault(code,set()).update(adverse)
                self.watches={code:{**row,'issues':list(scan.source_issues),'entry_blocked':True}
                    for code,row in previous.items() if not extra.get(code)}
            for key, intent in self.pending(side="BUY").items():
                stale=self.watches.get(intent['symbol'],{}).get('issues')
                if intent["symbol"] not in self.watches or stale:
                    self.inactive.add(key)
                    self.decision(at, intent["symbol"], "SKIPPED", 'STALE_RANKINGS' if stale else "CANDIDATE_REMOVED", intent_id=key)
            self.meta["scan_history"].append({"at": instant(at).isoformat(), **asdict(scan)})
        else:
            normalized = _observation(event, self.config.quality) if kind == "MARKET" else None
            event_id, _ = self.emit(kind, data, at)
            if normalized is not None and normalized.kind in {"candles", "trade", "orderbook"}:
                normalized = _observation(event, self.config.quality, event_id=event_id)
                self.strategy.on_event(normalized)
                reason = f"{normalized.kind}:{normalized.symbol}"
                if not normalized.issues and reason in self.ledger.state()["risk"]["reasons"].get("DATA_DEGRADED", []):
                    self.emit("RECOVER", {"lock":"DATA_DEGRADED", "reason":reason, "verified":True}, at)
                if normalized.kind == "orderbook":
                    self.books[normalized.symbol] = normalized
                    self.refresh_watch_quote(normalized, at)
                    self.fill_pending(normalized, at)
        if kind in {"RANKINGS", "MARKET"}:
            self.recover_retired_feeds(at)
        if ("MARK_UNCERTAIN" in self.ledger.state()["risk"]["reasons"].get("DATA_DEGRADED", [])
                and self.ledger.valuation(at)["status"] == "KNOWN"):
            self.emit("RECOVER", {"lock":"DATA_DEGRADED", "reason":"MARK_UNCERTAIN", "verified":True}, at)
        self.evaluate(at)

    def checkpoint(self, at, *, latest_only=False):
        # Persist structured diagnostics inside the existing generic CLOCK
        # envelope. Reopened reports need no sidecar or ledger schema changes.
        if latest_only:
            self.trim_live_history()
            saved=deepcopy({**self.meta,'inactive_intents':sorted(self.inactive)})
            saved["_checkpoint_sequence"] = self.sequence
            payload=canonical(saved)
            for signals,scans in ((100,10),(50,5)):
                if not self.compact_live or len(payload.encode('utf-8'))<=190000:break
                self.trim_live_history(signal_limit=signals,scan_limit=scans)
                saved=deepcopy({**self.meta,'inactive_intents':sorted(self.inactive),'_checkpoint_sequence':self.sequence})
                payload=canonical(saved)
            with self.ledger.connection:
                self.ledger.connection.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", ("_bot_summary", payload))
            self.ledger.meta["_bot_summary"] = saved
            return
        saved = json.loads(canonical({**self.meta, "inactive_intents": sorted(self.inactive)}))
        self.emit("CLOCK", {"_bot_summary": saved}, at)

    def run(self, events, risk_meta):
        for event in events:
            self.step(event, risk_meta)
        self.checkpoint(events[-1]["at"])
        return build_trading_report(self.ledger)


def run_paper_session(dataset, root):
    config = BotConfig.from_dataset(dataset)
    events = _events(dataset)
    Path(root).mkdir(parents=True, exist_ok=True)
    ledger = DaytradeLedger.create(root, initial_capital=config.initial_capital,
        quality=config.quality, policy_version=config.policy_version, synthetic=True,
        costs=config.fixture_costs, replay_seed=config.seed)
    try:
        session = _PaperSession(ledger, config, digest(dataset))
        for key in ("cost_policy_status", "stream_provenance", "paper_ready"):
            if key in dataset:
                session.meta[key] = dataset[key]
        return session.run(events, dataset.get("risk_meta"))
    finally:
        ledger.close()
