"""Pure OBSERVE_ONLY bridges and daily quality counts; no clocks, I/O or policies.

Use websocket_event inside WebSocketAdapter.on_message with caller-provided event
IDs and receive clocks. Pass the REST rankings result/envelope to rankings_event.
The report measures recorded quality flags, not execution permission or uptime.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from itertools import chain

from . import MODE
from .contracts import KST, digest, identifier, instant, symbol
from .http import RateGroup
from .market import MarketEvent, normalize


def websocket_event(frame, *, event_id, received_at, received_monotonic, session_id, quality):
    """Only public KR trade/orderbook messages; subscription auth stays in the adapter."""
    if not isinstance(frame, dict) or frame.get("type") != "message":
        raise ValueError("websocket market message required")
    topic, payload = frame.get("topic"), frame.get("data")
    parts = topic.split(":") if isinstance(topic, str) else []
    if len(parts) != 3 or parts[0] not in {"trade", "orderbook"} or parts[1] != "kr":
        raise ValueError("public KR trade/orderbook topic required")
    code = symbol(parts[2])
    if not isinstance(payload, dict) or ("symbol" in payload and payload["symbol"] != code):
        raise ValueError("topic and payload issuer mismatch")
    return normalize(parts[0], payload, event_id=event_id, received_at=received_at,
        received_monotonic=received_monotonic, session_id=session_id, quality=quality,
        issuer=code, source="TOSS_WEBSOCKET")


def rankings_event(snapshot, *, event_id, received_at, received_monotonic, session_id, quality):
    """Accept a decoded /rankings envelope or its result; never fetch a snapshot."""
    if not isinstance(snapshot, dict) or "error" in snapshot:
        raise ValueError("successful REST rankings snapshot required")
    payload = snapshot["result"] if "result" in snapshot else snapshot
    return normalize("rankings", payload, event_id=event_id, received_at=received_at,
        received_monotonic=received_monotonic, session_id=session_id, quality=quality,
        source="TOSS_REST_RANKINGS")


@dataclass(frozen=True)
class RuntimeSample:
    event_id: str
    received_at_utc: str
    group: str
    status: str
    entry_blocked: bool
    issuer: str = "ALL"

    def __post_init__(self):
        identifier(self.event_id)
        object.__setattr__(self, "received_at_utc", instant(self.received_at_utc).isoformat())
        object.__setattr__(self, "group", str(RateGroup(self.group)))
        if self.issuer != "ALL":
            symbol(self.issuer)
        if (self.status not in {"OK", "BACKOFF", "RATE_LIMIT_DEGRADED", "HTTP_ERROR", "TRANSPORT_ERROR"}
                or type(self.entry_blocked) is not bool
                or (self.status == "RATE_LIMIT_DEGRADED" and not self.entry_blocked)):
            raise ValueError("valid runtime quality status required")

    @property
    def fingerprint(self):
        return digest(asdict(self))


def runtime_sample(result, *, event_id, received_at, issuer="ALL"):
    """Keep status flags only; discard response payloads and arbitrary diagnostics."""
    if not isinstance(result, dict):
        raise ValueError("runtime result required")
    return RuntimeSample(event_id, received_at, result.get("group"), result.get("status"),
                         result.get("entry_blocked"), issuer)


_ISSUES = ("SOURCE_TIMESTAMP_UNAVAILABLE", "EMPTY_ORDERBOOK", "CROSSED_ORDERBOOK", "RATE_LIMIT_DEGRADED")


def _bucket():
    return {"total": 0, "blocked": 0, "by_kind": Counter(), "issues": Counter({key: 0 for key in _ISSUES})}


def _add(bucket, kind, issues, blocked):
    bucket["total"] += 1
    bucket["blocked"] += int(blocked)
    bucket["by_kind"][kind] += 1
    bucket["issues"].update(issues)


def _finish(bucket):
    total, blocked = bucket["total"], bucket["blocked"]
    return {"observations": total, "by_kind": dict(sorted(bucket["by_kind"].items())),
            "issue_counts": dict(sorted(bucket["issues"].items())),
            "stale_total": sum(n for key, n in bucket["issues"].items() if key.startswith("STALE_")),
            "entry_blocked": {"count": blocked, "total": total, "ratio": blocked / total if total else None}}


def observation_report(events, *, day, runtime_samples=()):
    """Summarize a KST day without estimating time-weighted availability.

    A rankings snapshot counts once globally and once per member issuer in
    per_symbol. Runtime ALL samples are global only. Duplicate event IDs with
    equal content are ignored; different IDs are separate observations even if
    payloads match (the LOSSY source has no trade sequence).
    """
    selected = date.fromisoformat(day) if isinstance(day, str) else day
    if type(selected) is not date:
        raise ValueError("explicit KST date required")
    overall, market, runtime = _bucket(), _bucket(), _bucket()
    per_symbol, seen = {}, {}
    duplicates, excluded = 0, 0
    times = []
    for sample in chain(events, runtime_samples):
        if not isinstance(sample, (MarketEvent, RuntimeSample)):
            raise ValueError("normalized market events or runtime samples required")
        prior = seen.get(sample.event_id)
        if prior is not None:
            if prior != sample.fingerprint:
                raise ValueError("conflicting observation event identity")
            duplicates += 1
            continue
        seen[sample.event_id] = sample.fingerprint
        received = instant(sample.received_at_utc)
        if received.astimezone(KST).date() != selected:
            excluded += 1
            continue
        times.append(received.isoformat())
        if isinstance(sample, MarketEvent):
            kind, issues = sample.kind, sample.issues
            group = market
            issuers = ([r["symbol"] for r in sample.data["rankings"]] if kind == "rankings" else
                       [] if sample.symbol == "ALL" else [sample.symbol])
        else:
            kind = "runtime:" + sample.group
            issues = (sample.status,) if sample.status != "OK" else ()
            group = runtime
            issuers = [] if sample.issuer == "ALL" else [sample.issuer]
        _add(overall, kind, issues, sample.entry_blocked)
        _add(group, kind, issues, sample.entry_blocked)
        for code in issuers:
            _add(per_symbol.setdefault(code, _bucket()), kind, issues, sample.entry_blocked)
    return {"mode": MODE, "execution_enabled": False, "paper_ready": False,
            "day_kst": selected.isoformat(), "timezone": "Asia/Seoul", **_finish(overall),
            "market": _finish(market), "runtime": _finish(runtime),
            "per_symbol": {code: _finish(bucket) for code, bucket in sorted(per_symbol.items())},
            "first_received_at_utc": min(times) if times else None,
            "last_received_at_utc": max(times) if times else None,
            "duplicates_ignored": duplicates, "excluded_other_days": excluded,
            "entry_blocked_ratio_basis": "RECORDED_QUALITY_SAMPLES_NOT_EXECUTION_PERMISSION_OR_UPTIME",
            "ranking_count_basis": "ONE_SNAPSHOT_GLOBALLY_ONE_MEMBERSHIP_PER_ISSUER",
            "runtime_count_basis": "RECORDED_STATUS_SAMPLES_NOT_OUTAGE_EPISODES",
            "source_loss_detection": "UNAVAILABLE_NO_SEQUENCE"}
