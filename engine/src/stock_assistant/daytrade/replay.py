"""Explicit-sequence replay; synthetic fills and observation-only inputs stay apart."""
from __future__ import annotations

from dataclasses import asdict
import json
import random

from .contracts import canonical, digest, instant
from .http import GroupState, HttpResult, PATH_GROUP, RestAdapter
from .ledger import DaytradeLedger, FixtureCosts
from .market import QualityPolicy


class ReplayEngine:
    def __init__(self, ledger, *, seed):
        if type(seed) is not int or seed < 0:
            raise ValueError("explicit non-negative random seed required")
        if ledger.meta.get("replay_seed") != seed:
            raise ValueError("replay seed must match immutable ledger metadata")
        self.ledger, self.seed = ledger, seed

    def apply(self, envelope):
        raw = json.loads(canonical(envelope))
        raw["at"] = instant(raw["at"]).isoformat()
        raw_hash = digest(raw)
        row = self.ledger.connection.execute("SELECT payload FROM events WHERE id=?", (raw["id"],)).fetchone()
        if row:
            if json.loads(row[0])["data"].get("_input_hash") != raw_hash:
                raise ValueError("conflicting replay input identity")
            return {"duplicate": True}
        event = json.loads(canonical(raw))
        event["data"]["_input_hash"] = raw_hash
        if event["kind"] == "HTTP_RESPONSE":
            if not self.ledger.meta["synthetic"]:
                raise ValueError("HTTP fixtures require a synthetic dataset")
            d = event["data"]
            group = PATH_GROUP.get(d["path"])
            if group is None:
                raise ValueError("public fixture path required")
            # Per-event seed makes resumed runs identical without serializing a
            # Python PRNG implementation's private getstate representation.
            rng = random.Random(int(digest({"seed": self.seed, "id": raw["id"]}), 16))
            adapter = RestAdapter(lambda *args: HttpResult(d["status"], d.get("payload", {}), d.get("headers", {})), rng=rng)
            for name, state in self.ledger.state()["runtime_groups"].items():
                adapter.groups[type(group)(name)] = GroupState(**state)
            result = adapter.fetch(d["path"], {}, now=d["monotonic"], purpose=d.get("purpose", "OBSERVE"))
            result.pop("payload", None)
            event.update(kind="RUNTIME_STATUS", data={"_input_hash": raw_hash, "group": str(group),
                "group_state": asdict(adapter.groups[group]), "result": result, "seed": self.seed})
        return self.ledger.apply(event)

    def run(self, events):
        if not isinstance(events, list):
            raise ValueError("event array required")
        indexed = {}
        for event in events:
            seq = event.get("sequence")
            if type(seq) is not int or seq < 0:
                raise ValueError("explicit event sequence required")
            if seq in indexed and canonical(indexed[seq]) != canonical(event):
                raise ValueError("ambiguous event sequence")
            indexed[seq] = event
        ordered = sorted(events, key=lambda e: (instant(e["at"]), e["sequence"]))
        for event in ordered:
            self.apply(event)
        return {"mode": "OBSERVE_ONLY", "paper_ready": False, "seed": self.seed,
                "ledger_hash": self.ledger.state_hash, "cash_audit": self.ledger.audit(),
                "events": len(self.ledger.snapshot()["events"]), "fills": len(self.ledger.snapshot()["fills"])}


def replay_dataset(dataset, root):
    if not isinstance(dataset, dict) or dataset.get("dataset_kind") not in {"SYNTHETIC", "OBSERVATION"}:
        raise ValueError("explicit SYNTHETIC or OBSERVATION dataset kind required")
    if type(dataset.get("seed")) is not int or dataset["seed"] < 0:
        raise ValueError("explicit seed required")
    synthetic = dataset["dataset_kind"] == "SYNTHETIC"
    if not synthetic and dataset.get("fixture_costs") is not None:
        raise ValueError("observation dataset cannot enable fixture costs")
    costs = FixtureCosts(**dataset["fixture_costs"]) if dataset.get("fixture_costs") is not None else None
    ledger = DaytradeLedger.create(root, initial_capital=dataset["initial_capital"],
        quality=QualityPolicy(**dataset["quality"]), policy_version=dataset["policy_version"],
        synthetic=synthetic, costs=costs, replay_seed=dataset["seed"])
    try:
        report = ReplayEngine(ledger, seed=dataset["seed"]).run(dataset["events"])
        report["dataset_hash"] = digest(dataset)
        report["report_hash"] = digest(report)
        return report
    finally:
        ledger.close()
