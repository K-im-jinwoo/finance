"""Public Toss format -> existing offline paper bot. This module opens no sockets.

SYNTHETIC means fabricated source observations. RECORDED means caller-supplied
captures; execution remains synthetic in both cases. PAPER_READY additionally
requires the explicitly approved cost profile; it never enables real orders.
Receive timestamps are explicit; a live callback may use caller-injected clocks.
"""
from __future__ import annotations

from dataclasses import asdict
from collections import Counter
from datetime import timedelta
import json
import time
from pathlib import Path

from . import POLICY_VERSION
from .bot import BotConfig, _PaperSession, run_paper_session
from .contracts import KST, canonical, digest, identifier, instant, monotonic_value, number, symbol
from .ledger import DaytradeLedger, FixtureCosts
from .market import QualityPolicy, normalize
from .observation import rankings_event, runtime_sample, websocket_event
from .report import APPROVED_COST_STATUS, PROVISIONAL_COST_STATUS, build_trading_report
from .scanner import ScannerConfig, risk_reasons
from .strategy import StrategyConfig

STAGE4_COST_PROFILE_STATUS = PROVISIONAL_COST_STATUS
STREAM_FORMAT = "TOSS_PUBLIC_STREAM_V1"


def provisional_costs():
    return FixtureCosts("STAGE4-DEV-CONSERVATIVE-V1", "2.0", "2.0", "20", "2", "0.5", 200)


def approved_costs():
    return FixtureCosts("STAGE5-APPROVED-CONSERVATIVE-V1", "2.0", "2.0", "20", "2", "0.5", 200)


def _approved_settings(payload):
    if not isinstance(payload, dict) or payload.get("orders_enabled", False) is not False:
        raise ValueError("real orders are forbidden")
    supplied = payload.get("fixture_costs", {})
    if isinstance(supplied, FixtureCosts):
        supplied = asdict(supplied)
    expected = asdict(approved_costs())
    try:
        candidate = FixtureCosts(**(expected | supplied))
    except (ValueError, TypeError):
        raise ValueError("approved costs required") from None
    if asdict(candidate) != expected:
        raise ValueError("approved cost policy cannot be overridden")
    # The provisional converter keeps its existing paper_ready=False boundary.
    return {**payload, "fixture_costs":expected, "paper_ready":False}


def provisional_quality():
    return QualityPolicy(5, 45, 90)


def provisional_scanner_config(quality=None):
    quality = quality or provisional_quality()
    return ScannerConfig(min_price="1000", min_turnover="100000000", max_spread_bps="50",
        max_watch=10, quote_max_age_seconds=quality.quote_max_age_seconds,
        ranking_max_age_seconds=quality.ranking_max_age_seconds)


def provisional_strategy_config(quality=None):
    quality = quality or provisional_quality()
    # Reuse the existing synthetic bot sample's thresholds, not production policy.
    return StrategyConfig(min_candles=4, volume_spike_ratio="2", min_bid_volume="100",
        max_spread_bps="50", quote_max_age_seconds=quality.quote_max_age_seconds,
        candle_max_age_seconds=quality.candle_max_age_seconds, take_profit_bps="500", stop_loss_bps="300")


def _result(payload):
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("successful public REST payload required")
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        raise ValueError("public REST result object required")
    return result


def _only(row, keys):
    if not isinstance(row, dict):
        raise ValueError("public market object required")
    return {key: row[key] for key in keys if key in row}


def _clean_record(row, index, quality):
    if not isinstance(row, dict) or row.get("scope", "INTEGRATED_KR") != "INTEGRATED_KR":
        raise ValueError("integrated KR stream record required")
    at = instant(row["at"])
    event_id = identifier(row.get("id", f"stream-{index:08d}"))
    sequence = row.get("sequence", index)
    if type(sequence) is not int or sequence < 0:
        raise ValueError("non-negative stream sequence required")
    mono = row.get("received_monotonic", index)
    monotonic_value(mono)
    kind = row.get("kind")
    clean = {"id":event_id, "sequence":sequence, "at":at.isoformat(), "kind":kind,
             "received_monotonic":mono}
    kwargs = {"event_id":event_id, "received_at":at, "received_monotonic":mono,
              "session_id":at.astimezone(KST).date().isoformat(), "quality":quality}
    if kind == "ws_frame":
        frame = row["frame"]
        if not isinstance(frame, dict) or not isinstance(frame.get("topic"), str):
            raise ValueError("public WS market message required")
        parts = frame["topic"].split(":")
        if len(parts) != 3 or parts[0] not in {"trade", "orderbook"} or parts[1] != "kr":
            raise ValueError("public KR trade/orderbook topics only")
        channel = frame["topic"].split(":")[0]
        keys = ("currency", "timestamp", "price", "volume", "symbol") if channel=="trade" else (
            "currency", "timestamp", "asks", "bids", "symbol")
        data = _only(frame["data"], keys)
        if channel == "orderbook":
            for side in ("asks", "bids"):
                data[side] = [_only(level, ("price", "volume")) for level in data[side]]
        public = {"type":frame.get("type"), "topic":frame["topic"], "data":data}
        websocket_event(public, **kwargs)
        clean["frame"] = public
    elif kind == "candles":
        result = _result(row["payload"])
        rows = result.get("candles")
        if not isinstance(rows, list):
            raise ValueError("public candles array required")
        public = {"candles":[_only(bar, ("currency", "timestamp", "openPrice", "highPrice",
                                         "lowPrice", "closePrice", "volume")) for bar in rows]}
        normalize("candles", public, issuer=row["symbol"], **kwargs)
        source = row.get("source", "TOSS_REST_CANDLES")
        if source not in {"TOSS_REST_CANDLES", "TOSS_WS_COMPLETED_1M"}:
            raise ValueError("known candle source required")
        clean.update(symbol=row["symbol"], payload={"result":public})
        if source != "TOSS_REST_CANDLES":
            clean["source"] = source
    elif kind == "rankings":
        result = _result(row["payload"])
        rows = result.get("rankings")
        if not isinstance(rows, list):
            raise ValueError("public rankings array required")
        public_rows, ranks = [], set()
        for index, entry in enumerate(rows, 1):
            item = _only(entry, ("rank", "symbol", "currency", "price", "tradingAmount", "tradingVolume", "name"))
            rank = item.get("rank", index)
            if type(rank) is not int or not 1 <= rank <= 100 or rank in ranks:
                raise ValueError("unique valid market ranking positions required")
            ranks.add(rank)
            if "name" in item and not isinstance(item["name"], str):
                raise ValueError("ranking name must be text")
            if "price" in item:
                item["price"] = _only(item["price"], ("lastPrice", "basePrice", "changeRate"))
                number(item["price"].get("lastPrice"), positive=True)
            public_rows.append(item)
        public = {"rankedAt":result.get("rankedAt"), "rankings":public_rows}
        rankings_event(public, **kwargs)
        ranking_type = row.get("ranking_type", "MARKET_TRADING_AMOUNT")
        if ranking_type not in {"MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME"}:
            raise ValueError("market-wide turnover or volume rankings required")
        clean.update(payload={"result":public}, ranking_type=ranking_type)
        if "risk_meta" in row:
            if not isinstance(row["risk_meta"], dict):
                raise ValueError("symbol-keyed risk metadata required")
            clean["risk_meta"] = {code:_only(meta, ("name", "halted", "delisting", "warning_or_caution",
                "major_negative_disclosure", "market", "securityType", "isCommonShare", "leverageFactor",
                "status", "sharesOutstanding", "koreanMarketDetail", "metadata_status", "metadata_valid_until"))
                for code,meta in row["risk_meta"].items()}
            for code, meta in clean["risk_meta"].items():
                symbol(code)
                risk_reasons(meta)
                if "name" in meta and not isinstance(meta["name"], str):
                    raise ValueError("risk metadata name must be text")
    elif kind == "session":
        verified = row.get("data", {}).get("normal_session_verified", False)
        if type(verified) is not bool:
            raise ValueError("explicit calendar verification boolean required")
        clean["data"] = {"normal_session_verified":verified}
    elif kind == "runtime_status":
        data = row["data"]
        sample = runtime_sample(data, event_id=event_id, received_at=at)
        if sample.status != "OK" and not sample.entry_blocked:
            raise ValueError("failed transport must block entry")
        clean["data"] = {"group":sample.group, "status":sample.status, "entry_blocked":sample.entry_blocked}
    elif kind in {"clock", "end_session"}:
        clean["data"] = {}
    else:
        raise ValueError("unsupported public stream kind")
    return json.loads(canonical(clean))


def _configuration(payload):
    if (not isinstance(payload, dict) or payload.get("stream_format", STREAM_FORMAT)!=STREAM_FORMAT
            or payload.get("orders_enabled", False) is not False or payload.get("paper_ready", False) is not False):
        raise ValueError("offline public stream format required")
    source_kind = payload.get("stream_kind", "SYNTHETIC")
    if source_kind not in {"SYNTHETIC", "RECORDED"}:
        raise ValueError("explicit synthetic or recorded source kind required")
    quality = QualityPolicy(**(asdict(provisional_quality()) | payload.get("quality", {})))
    costs = FixtureCosts(**(asdict(provisional_costs()) | payload.get("fixture_costs", {})))
    scanner = ScannerConfig(**(asdict(provisional_scanner_config(quality)) | payload.get("scanner_config", {})))
    strategy = StrategyConfig(**(asdict(provisional_strategy_config(quality)) | payload.get("strategy_config", {})))
    return source_kind, quality, costs, scanner, strategy


def _to_bot_event(row, rankings, risk_metadata):
    """Stateful snapshot merge shared by batch conversion and live callbacks."""
    event = {key:row[key] for key in ("id", "sequence", "at")}
    kind = row["kind"]
    if kind == "ws_frame":
        frame = row["frame"]
        channel, _, code = frame["topic"].split(":")
        event.update(kind="MARKET", data={"feed":channel, "symbol":code,
            "source":"TOSS_WEBSOCKET", "payload":frame["data"], "received_monotonic":row["received_monotonic"]})
    elif kind == "candles":
        event.update(kind="MARKET", data={"feed":"candles", "symbol":row["symbol"],
            "source":row.get("source", "TOSS_REST_CANDLES"), "payload":row["payload"]["result"], "received_monotonic":row["received_monotonic"]})
    elif kind == "rankings":
        rankings[row["ranking_type"]] = {"scope":"INTEGRATED_KR", **row["payload"]["result"]}
        for code, update in row.get("risk_meta", {}).items():
            risk_metadata[code] = risk_metadata.get(code, {}) | update
        turnover = rankings.get("MARKET_TRADING_AMOUNT")
        if turnover is None:
            event.update(kind="CLOCK", data={})
        else:
            event.update(kind="RANKINGS", data={"turnover":turnover,
                "volume":rankings.get("MARKET_TRADING_VOLUME"), "risk_meta":dict(risk_metadata)})
    elif kind == "runtime_status":
        data = row["data"]
        if data["status"] == "OK" and not data["entry_blocked"]:
            event.update(kind="CLOCK", data={})
        else:
            lock = "RATE_LIMIT_DEGRADED" if data["status"]=="RATE_LIMIT_DEGRADED" else "DATA_DEGRADED"
            event.update(kind="LOCK", data={"lock":lock, "reason":"HTTP:"+data["group"]})
    else:
        event.update(kind=kind.upper(), data=row["data"])
    return event


def stream_to_dataset(stream_payload):
    source_kind, quality, costs, scanner, strategy = _configuration(stream_payload)
    rows = stream_payload.get("events")
    if not isinstance(rows, list) or not rows:
        raise ValueError("nonempty public stream event array required")
    events, seen, cleaned, rankings, risk_metadata = [], {}, [], {}, {}
    previous = None
    for index, raw in enumerate(rows):
        row = _clean_record(raw, index, quality)
        fingerprint = digest(row)
        if row["id"] in seen:
            if seen[row["id"]] != fingerprint:
                raise ValueError("conflicting stream event identity")
            continue
        pair = (instant(row["at"]), row["sequence"])
        if previous is not None and (pair[0]<previous[0] or pair[1]<=previous[1]):
            raise ValueError("stream time and sequence must advance")
        previous, seen[row["id"]] = pair, fingerprint
        cleaned.append(row)
        events.append(_to_bot_event(row, rankings, risk_metadata))
    return {"dataset_kind":"SYNTHETIC", "initial_capital":stream_payload.get("initial_capital", "10000000"),
        "seed":stream_payload.get("seed", 42), "policy_version":stream_payload.get("policy_version", POLICY_VERSION),
        "quality":asdict(quality), "fixture_costs":asdict(costs), "scanner_config":asdict(scanner),
        "strategy_config":asdict(strategy), "events":events, "cost_policy_status":STAGE4_COST_PROFILE_STATUS,
        "stream_provenance":{"kind":source_kind, "format":STREAM_FORMAT, "source_hash":digest(cleaned)},
        **{key:stream_payload[key] for key in ('max_daily_entries','reentry_cooldown_seconds') if key in stream_payload}}


def run_observed_stream(stream_payload, root):
    return run_paper_session(stream_to_dataset(stream_payload), root)


def run_paper_ready_stream(stream_payload, root):
    dataset = stream_to_dataset(_approved_settings(stream_payload))
    dataset.update(cost_policy_status=APPROVED_COST_STATUS, paper_ready=True)
    return run_paper_session(dataset, root)


class LiveSessionBridge:
    """Collect callback data, then replay through exactly the same offline path.

    on_frame may be assigned to WebSocketAdapter.on_message. Supply receive_clock
    and monotonic_clock when the callback doesn't pass its receive timestamps.
    on_rest accepts RestAdapter.fetch results; neither method fetches anything.
    """
    orders_enabled = False
    paper_ready = False

    def __init__(self, *, receive_clock=None, monotonic_clock=None, **settings):
        allowed = {"initial_capital", "seed", "stream_kind", "quality", "fixture_costs",
                   "scanner_config", "strategy_config", "policy_version", "max_daily_entries", "reentry_cooldown_seconds"}
        if set(settings)-allowed:
            raise ValueError("unsupported offline capture settings")
        self.settings = json.loads(canonical(settings))
        self.quality = _configuration(self.settings)[1]
        self.receive_clock, self.monotonic_clock = receive_clock, monotonic_clock
        self._events, self._seen = [], {}

    def add(self, record):
        clean = _clean_record(record, len(self._events), self.quality)
        return self._capture(clean)

    def _capture(self, clean):
        fingerprint = digest(clean)
        if clean["id"] in self._seen:
            if self._seen[clean["id"]] != fingerprint:
                raise ValueError("conflicting captured event identity")
            return False
        if self._events and (instant(clean["at"])<instant(self._events[-1]["at"])
                             or clean["sequence"]<=self._events[-1]["sequence"]):
            raise ValueError("capture time and sequence must advance")
        self._seen[clean["id"]] = fingerprint
        self._events.append(clean)
        return True

    def _receive(self, at, received_monotonic):
        at = at if at is not None else self.receive_clock() if self.receive_clock is not None else None
        if at is None:
            raise ValueError("receive timestamp or injected clock required")
        mono = received_monotonic if received_monotonic is not None else (
            self.monotonic_clock() if self.monotonic_clock is not None else len(self._events))
        return {"at":instant(at).isoformat(), "received_monotonic":mono}

    def on_frame(self, frame, *, at=None, received_monotonic=None):
        return self.add({"kind":"ws_frame", "frame":frame, **self._receive(at, received_monotonic)})

    def on_rest(self, path, response, *, at=None, received_monotonic=None, symbol=None,
                ranking_type=None, risk_meta=None):
        groups = {"/api/v1/rankings":"RANKINGS", "/api/v1/candles":"MARKET_DATA_CHART"}
        if path not in groups or not isinstance(response, dict):
            raise ValueError("public rankings/candles adapter result required")
        receive = self._receive(at, received_monotonic)
        if response.get("group", groups[path]) != groups[path]:
            raise ValueError("REST adapter group mismatch")
        if response.get("status", "OK") != "OK" or response.get("entry_blocked"):
            return self.add({"kind":"runtime_status", **receive, "data":{
                "group":groups[path], "status":response["status"], "entry_blocked":True}})
        record = {"kind":"rankings" if path=="/api/v1/rankings" else "candles",
                  **receive, "payload":response.get("payload", response)}
        if record["kind"] == "rankings":
            record["ranking_type"] = ranking_type or "MARKET_TRADING_AMOUNT"
            if risk_meta is not None: record["risk_meta"] = risk_meta
        else: record["symbol"] = symbol
        return self.add(record)

    def to_stream(self):
        return json.loads(canonical({**self.settings, "stream_format":STREAM_FORMAT, "events":self._events}))

    def to_dataset(self):
        return stream_to_dataset(self.to_stream())

    def run(self, root):
        return run_observed_stream(self.to_stream(), root)


class LiveCandleBuilder:
    """Completed observed-trade bars; never invent coverage while unsubscribed.

    End instants are minute-aligned. A partial initial/re-entry minute is omitted.
    Zero-observed-trade bars use the last observed close only while subscribed.
    A grace watermark allows bounded late frames before an immutable bar seals.
    No provider sequence exists, so this is not a certified complete tape.
    """
    def __init__(self, *, max_bars=20, grace_seconds=0):
        if type(max_bars) is not int or not 1 <= max_bars <= 20:
            raise ValueError("one to twenty completed bars required")
        monotonic_value(grace_seconds)
        if grace_seconds > 30:
            raise ValueError("bounded completion grace required")
        self.max_bars, self.grace = max_bars, timedelta(seconds=grace_seconds)
        self.day, self.states, self.counts = None, {}, Counter()

    def _day(self, at):
        day = at.astimezone(KST).date()
        if self.day is not None and day < self.day:
            raise ValueError("candle receive day cannot regress")
        if day != self.day:
            self.day, self.states = day, {}

    @staticmethod
    def _end(at):
        return at.replace(second=0, microsecond=0) + timedelta(minutes=1)

    def set_active(self, symbols, *, at):
        received = instant(at)
        self._day(received)
        active = {symbol(code) for code in symbols}
        for code in active:
            state = self.states.setdefault(code, {"active":False, "bars":[], "pending":{},
                "last_close":None, "observed_trades":0, "completed":0, "flat_bars":0})
            if not state["active"]:
                state.update(active=True, since=received, next_end=self._end(received), pending={})
        for code, state in self.states.items():
            if code not in active:
                state["active"] = False

    def update(self, frame, *, at):
        received = instant(at)
        self._day(received)
        parts = frame.get("topic", "").split(":")
        if frame.get("type") != "message" or len(parts) != 3 or parts[:2] != ["trade", "kr"]:
            raise ValueError("public KR trade message required")
        code, data = symbol(parts[2]), frame["data"]
        if data.get("currency") != "KRW":
            raise ValueError("KRW trade required")
        stamp, price, volume = instant(data["timestamp"]), number(data["price"], positive=True), number(data["volume"])
        self.counts["trade_frames_seen"] += 1
        if stamp > received or stamp.astimezone(KST).date() != self.day:
            self.counts["future_or_other_day"] += 1
            return False
        if code not in self.states:
            self.set_active({c for c,s in self.states.items() if s["active"]} | {code}, at=received)
        state = self.states[code]
        end = self._end(stamp)
        if not state["active"] or end < state["next_end"]:
            self.counts["late_or_inactive"] += 1
            return False
        bucket = state["pending"].setdefault(end, {"first":stamp, "last":stamp,
            "open":price, "high":price, "low":price, "close":price, "volume":0})
        if stamp < bucket["first"]:
            bucket.update(first=stamp, open=price)
        if stamp >= bucket["last"]:
            bucket.update(last=stamp, close=price)
        bucket.update(high=max(bucket["high"],price), low=min(bucket["low"],price), volume=bucket["volume"]+volume)
        state["observed_trades"] += 1
        return True

    def advance(self, *, at):
        received = instant(at)
        self._day(received)
        watermark, changed = received - self.grace, {}
        for code, state in self.states.items():
            if not state["active"]:
                continue
            emitted = False
            while state["next_end"] <= watermark:
                end = state["next_end"]
                bucket = state["pending"].pop(end, None)
                state["next_end"] += timedelta(minutes=1)
                partial = end - timedelta(minutes=1) < state["since"]
                if bucket is not None:
                    state["last_close"] = bucket["close"]
                elif state["last_close"] is not None:
                    price = state["last_close"]
                    bucket = {"open":price,"high":price,"low":price,"close":price,"volume":0}
                    state["flat_bars"] += 1
                if partial:
                    self.counts["partial_minutes_omitted"] += 1
                    continue
                if bucket is None:
                    continue
                bar = {"timestamp":end.isoformat(), "currency":"KRW", "volume":format(number(bucket["volume"]), "f"),
                    **{raw:bucket[key] for key,raw in (("open","openPrice"),("high","highPrice"),("low","lowPrice"),("close","closePrice"))}}
                state["bars"].append(json.loads(canonical(bar)))
                state["bars"] = state["bars"][-self.max_bars:]
                state["completed"] += 1
                emitted = True
            if emitted:
                changed[code] = self.history(code)
        return changed

    def history(self, code):
        return json.loads(canonical(self.states.get(code, {}).get("bars", [])))

    def snapshot(self):
        return {"source":"TOSS_WS_COMPLETED_1M", "counts":dict(self.counts),
            "no_trade_basis":"ZERO_OBSERVED_TRADES_WHILE_SUBSCRIBED", "complete_tape":False,
            "per_symbol":{code:{"active":s["active"], "bar_count":len(s["bars"]),
                "completed":s["completed"], "observed_trades":s["observed_trades"],
                "flat_bars":s["flat_bars"], "bars":self.history(code)} for code,s in self.states.items()}}


def _orderbook_problem(data):
    try:
        sides = {}
        for side in ("bids", "asks"):
            rows = data[side]
            if not isinstance(rows, list) or len(rows) > 100:
                return "INVALID_ORDERBOOK"
            sides[side] = [number(r["price"], positive=True) for r in rows if number(r["volume"]) > 0]
        if not sides["bids"] or not sides["asks"]:
            return "EMPTY_ORDERBOOK"
        if max(sides["bids"]) >= min(sides["asks"]):
            return "CROSSED_ORDERBOOK"
    except (KeyError, TypeError, ValueError):
        return "INVALID_ORDERBOOK"
    return None


class LiveFrameBuffer:
    """Every observed trade feeds bars; only fresh latest prices reach SQLite."""
    def __init__(self, candle_builder, *, trade_interval=.75, book_interval=.4, max_age_seconds=3.5):
        for value in (trade_interval, book_interval, max_age_seconds):
            monotonic_value(value)
            if not value:
                raise ValueError("positive frame cadence required")
        self.candles = candle_builder
        self.intervals = {"trade":trade_interval,"orderbook":book_interval}
        self.max_age = max_age_seconds
        self.latest, self.last_emit, self.newest_stamp, self.counts = {}, {}, {}, Counter()

    def push(self, frame, *, at):
        received = instant(at)
        parts = frame.get("topic", "").split(":")
        if frame.get("type") != "message" or len(parts) != 3 or parts[0] not in self.intervals or parts[1] != "kr":
            raise ValueError("public trade/orderbook message required")
        kind, code, data = parts[0], symbol(parts[2]), frame["data"]
        if data.get("currency") != "KRW":
            raise ValueError("KRW frame required")
        self.counts["raw_" + kind] += 1
        if data.get("timestamp") is None:
            self.counts["MISSING_TIMESTAMP"] += 1
            return False
        if kind == "trade":
            self.candles.update(frame, at=received)
        else:
            problem = _orderbook_problem(data)
            if problem:
                self.counts[problem] += 1
                return False
        stamp = instant(data["timestamp"])
        age = (received - stamp).total_seconds()
        if age < 0 or age > self.max_age:
            self.counts["STALE_OR_FUTURE_FRAME"] += 1
            return False
        key = (kind, code)
        if key in self.newest_stamp and self.newest_stamp[key] > stamp:
            self.counts["OUT_OF_ORDER_PRICE"] += 1
            return False
        if key in self.latest:
            self.counts["COALESCED"] += 1
        self.newest_stamp[key] = stamp
        self.latest[key] = (frame, stamp)
        return True

    def drain(self, *, at, monotonic, pending_symbols=()):
        received = instant(at)
        monotonic_value(monotonic)
        frames = []
        ordered = sorted(self.latest.items(),
            key=lambda item: (0 if item[0][1] in pending_symbols and item[0][0] == "orderbook"
                              else 1 if item[0][1] in pending_symbols else 2))
        for key, (frame, stamp) in ordered:
            kind, code = key
            age = (received - stamp).total_seconds()
            if age < 0 or age > self.max_age:
                del self.latest[key]
                self.counts["STALE_OR_FUTURE_FRAME"] += 1
                continue
            immediate = kind == "orderbook" and code in pending_symbols
            if not immediate and monotonic - self.last_emit.get(key, -1e20) < self.intervals[kind]:
                continue
            frames.append(frame)
            self.last_emit[key] = monotonic
            del self.latest[key]
            self.counts["emitted_" + kind] += 1
        return frames


class LivePaperSession(LiveSessionBridge):
    """Incremental paper-only runner. Caller owns clocks and network adapters.

    A true slot flag is required even though this object never connects. Each
    callback commits a checkpoint before returning, so a separate read-only
    dashboard connection can inspect the latest completed callback immediately.
    """
    orders_enabled = False
    paper_ready = True

    def __init__(self, root, *, websocket_slot_available=False, fixture_costs=None,
                 ws_adapter=None, receive_clock=None, monotonic_clock=None,
                 checkpoint_interval_seconds=0,checkpoint_clock=None, **settings):
        if websocket_slot_available is not True:
            raise ValueError("explicit websocket slot confirmation required")
        options = _approved_settings({**settings, "fixture_costs":fixture_costs if fixture_costs is not None else {}})
        options.pop("paper_ready")
        super().__init__(receive_clock=receive_clock, monotonic_clock=monotonic_clock, **options)
        source_kind, quality, costs, scanner, strategy = _configuration(self.settings)
        config = BotConfig(self.settings.get("initial_capital", "10000000"),
            self.settings.get("policy_version", POLICY_VERSION), self.settings.get("seed", 42),
            quality, costs, scanner, strategy, self.settings.get('max_daily_entries'), self.settings.get('reentry_cooldown_seconds'))
        Path(root).mkdir(parents=True, exist_ok=True)
        self.ledger = DaytradeLedger.create(root, initial_capital=config.initial_capital,
            policy_version=config.policy_version, replay_seed=config.seed,
            quality=config.quality, synthetic=True, costs=config.fixture_costs)
        self.ws_adapter = ws_adapter
        self._rankings, self._risk_metadata = {}, {}
        self._closed = self._session_ended = False
        self._source_hash = digest([])
        if checkpoint_interval_seconds < 0:
            raise ValueError('nonnegative checkpoint interval required')
        self._checkpoint_interval=checkpoint_interval_seconds
        self._checkpoint_clock=checkpoint_clock or time.monotonic
        self._last_checkpoint=float('-inf')
        self._checkpoint_fills=0
        self._checkpoint_critical=0
        try:
            capability = {"cost_policy_status":APPROVED_COST_STATUS, "paper_ready":True,
                "profile":strategy.profile, "stream_provenance":{"kind":source_kind, "format":STREAM_FORMAT}}
            # Only this freshly-created isolated ledger gets the capability
            # metadata. No fabricated wall-clock event and no existing DB edit.
            with self.ledger.connection:
                self.ledger.connection.executemany("INSERT INTO meta VALUES(?,?)",
                    [(key, canonical(value)) for key,value in capability.items()])
            self.ledger.meta.update(json.loads(canonical(capability)))
            self._runner = _PaperSession(self.ledger, config, digest(self.settings), compact_live=True)
            self._runner.meta.update(capability)
        except Exception:
            self.ledger.close()
            raise

    def add(self, record):
        if self._closed:
            raise ValueError("paper session is closed")
        clean = _clean_record(record, len(self._events), self.quality)
        if clean["id"] in self._seen:
            return self._capture(clean)
        if self._session_ended:
            raise ValueError("paper trading day has ended")
        # Before discovery, keep the supported replay/warm-up observations.
        # Once a pool exists, in-flight frames from retired subscriptions must
        # not create session-wide quality locks (held names still need exits).
        if clean["kind"] in {"ws_frame", "candles"} and self._runner.meta["scan_history"]:
            code = clean["frame"]["topic"].rsplit(":", 1)[1] if clean["kind"] == "ws_frame" else clean["symbol"]
            if code not in self._runner.watches and code not in self.ledger.state()["positions"]:
                return False
        if clean["kind"] in {"ws_frame", "candles"} and clean["received_monotonic"] < self.ledger.state()["last_monotonic"]:
            raise ValueError("observation monotonic time regressed")
        self._capture(clean)
        event = _to_bot_event(clean, self._rankings, self._risk_metadata)
        self._runner.step(event)
        self._source_hash = digest({"previous":self._source_hash, "event":clean})
        self._runner.meta["stream_provenance"].update(source_hash=self._source_hash, hash_basis="ORDERED_EVENT_CHAIN")
        self._runner.meta["dataset_hash"] = self._source_hash
        checkpoint_now=self._checkpoint_clock()
        fills=self.ledger.connection.execute('SELECT count(*) FROM paper_fills').fetchone()[0]
        if (checkpoint_now-self._last_checkpoint>=self._checkpoint_interval or fills!=self._checkpoint_fills
                or self._runner.critical_sequence!=self._checkpoint_critical
                or clean['kind'] in {'session','rankings','end_session'}):
            self._runner.checkpoint(clean["at"], latest_only=True)
            self._last_checkpoint,self._checkpoint_fills=checkpoint_now,fills
            self._checkpoint_critical=self._runner.critical_sequence
        self._session_ended = clean["kind"] == "end_session"
        if clean["kind"] == "rankings" and self.ws_adapter is not None:
            # Preserve held names even when a fresh scan removes them: exits
            # still need quotes. Selected-only names are not execution approval.
            names = set(self._runner.watches) | set(self.ledger.state()["positions"])
            topics = {f"{channel}:kr:{code}" for channel in ("trade", "orderbook") for code in names}
            self.ws_adapter.replace(topics, now=clean["received_monotonic"])
        return True

    def on_session(self, *, normal_session_verified=False, at=None, received_monotonic=None):
        return self.add({"kind":"session", **self._receive(at, received_monotonic),
                         "data":{"normal_session_verified":normal_session_verified}})

    def on_clock(self, *, at=None, received_monotonic=None):
        return self.add({"kind":"clock", **self._receive(at, received_monotonic)})

    def close_session(self, *, at=None, received_monotonic=None):
        return self.add({"kind":"end_session", **self._receive(at, received_monotonic)})

    def report(self):
        if self._closed:
            raise ValueError("paper session is closed")
        return build_trading_report(self.ledger)

    def to_dataset(self):
        dataset = super().to_dataset()
        dataset.update(cost_policy_status=APPROVED_COST_STATUS, paper_ready=True)
        return dataset

    def run(self, root):
        raise ValueError("LivePaperSession processes callbacks immediately; use report()")

    def close(self):
        if not self._closed:
            self.ledger.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
