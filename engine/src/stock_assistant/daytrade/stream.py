"""Public Toss format -> existing offline paper bot. This module opens no sockets.

SYNTHETIC means fabricated source observations. RECORDED means caller-supplied
captures; execution remains synthetic in both cases and never becomes PAPER_READY.
Receive timestamps are explicit; a live callback may use caller-injected clocks.
"""
from __future__ import annotations

from dataclasses import asdict
import json

from . import POLICY_VERSION
from .bot import run_paper_session
from .contracts import KST, canonical, digest, identifier, instant, monotonic_value, number, symbol
from .ledger import FixtureCosts
from .market import QualityPolicy, normalize
from .observation import rankings_event, runtime_sample, websocket_event
from .report import PROVISIONAL_COST_STATUS
from .scanner import ScannerConfig, risk_reasons
from .strategy import StrategyConfig

STAGE4_COST_PROFILE_STATUS = PROVISIONAL_COST_STATUS
STREAM_FORMAT = "TOSS_PUBLIC_STREAM_V1"


def provisional_costs():
    return FixtureCosts("STAGE4-DEV-CONSERVATIVE-V1", "2.0", "2.0", "20", "2", "0.5", 200)


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
        clean.update(symbol=row["symbol"], payload={"result":public})
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
                                                   "major_negative_disclosure")) for code,meta in row["risk_meta"].items()}
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
        event = {key:row[key] for key in ("id", "sequence", "at")}
        kind = row["kind"]
        if kind == "ws_frame":
            frame = row["frame"]
            channel, _, code = frame["topic"].split(":")
            event.update(kind="MARKET", data={"feed":channel, "symbol":code,
                "source":"TOSS_WEBSOCKET", "payload":frame["data"], "received_monotonic":row["received_monotonic"]})
        elif kind == "candles":
            event.update(kind="MARKET", data={"feed":"candles", "symbol":row["symbol"],
                "source":"TOSS_REST_CANDLES", "payload":row["payload"]["result"], "received_monotonic":row["received_monotonic"]})
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
        events.append(event)
    return {"dataset_kind":"SYNTHETIC", "initial_capital":stream_payload.get("initial_capital", "10000000"),
        "seed":stream_payload.get("seed", 42), "policy_version":stream_payload.get("policy_version", POLICY_VERSION),
        "quality":asdict(quality), "fixture_costs":asdict(costs), "scanner_config":asdict(scanner),
        "strategy_config":asdict(strategy), "events":events, "cost_policy_status":STAGE4_COST_PROFILE_STATUS,
        "stream_provenance":{"kind":source_kind, "format":STREAM_FORMAT, "source_hash":digest(cleaned)}}


def run_observed_stream(stream_payload, root):
    return run_paper_session(stream_to_dataset(stream_payload), root)


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
                   "scanner_config", "strategy_config", "policy_version"}
        if set(settings)-allowed:
            raise ValueError("unsupported offline capture settings")
        self.settings = json.loads(canonical(settings))
        self.quality = _configuration(self.settings)[1]
        self.receive_clock, self.monotonic_clock = receive_clock, monotonic_clock
        self._events, self._seen = [], {}

    def add(self, record):
        clean = _clean_record(record, len(self._events), self.quality)
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
