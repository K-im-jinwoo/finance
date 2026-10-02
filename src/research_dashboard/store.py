import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .contracts import digest, validate_dataset
from .engine import Engine
from .rules import Policy

class Store:
    """This database is exclusively a local paper ledger, never the source DB."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY, dataset_json TEXT NOT NULL,
                    config_json TEXT NOT NULL, result_json TEXT NOT NULL, cursor INTEGER NOT NULL,
                    state TEXT NOT NULL, created_order INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_ledger (
                    experiment_id TEXT NOT NULL, event_id TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY (experiment_id, event_id)
                );
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def create(self, dataset: dict, policy: Policy) -> str:
        validate_dataset(dataset)
        # UUID avoids overwriting a result even with identical settings.
        eid = "E-" + digest({"dataset": digest(dataset), "policy": policy.snapshot(), "nonce": uuid4().hex})[:24]
        engine = Engine(dataset, policy)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            order = c.execute("SELECT COALESCE(MAX(created_order),0)+1 FROM experiments").fetchone()[0]
            c.execute("INSERT INTO experiments VALUES(?,?,?,?,?,?,?)", (eid, json.dumps(dataset, ensure_ascii=False), json.dumps(policy.snapshot()), json.dumps(engine.result(), ensure_ascii=False), 0, "READY", order))
        return eid

    def list(self):
        with self.connect() as c:
            return [{"experiment_id": row["experiment_id"], "state": row["state"], "cursor": row["cursor"],
                     "dataset_id": json.loads(row["result_json"])["coverage"]["dataset_id"], "cost_bps": json.loads(row["config_json"])["round_trip_bps"],
                     "mode": json.loads(row["result_json"])["coverage"]["mode"]} for row in c.execute("SELECT * FROM experiments ORDER BY created_order DESC")]

    def get(self, eid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM experiments WHERE experiment_id=?", (eid,)).fetchone()
            if row is None:
                raise KeyError(eid)
            result = json.loads(row["result_json"])
            result.update(experiment_id=eid, cursor=row["cursor"], total_events=len(json.loads(row["dataset_json"])["events"]))
            return result

    def apply(self, eid, action="advance", limit=1):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM experiments WHERE experiment_id=?", (eid,)).fetchone()
            if row is None:
                raise KeyError(eid)
            data, config = json.loads(row["dataset_json"]), json.loads(row["config_json"])
            config["proposals"] = tuple(config["proposals"])
            engine = Engine(data, Policy(**config))
            engine.s = json.loads(row["result_json"])["state"]
            cursor = row["cursor"]
            if action == "start":
                if engine.s["status"] != "READY":
                    raise ValueError("start requires READY")
                engine.s["status"] = "RUNNING"
            elif action in {"pause", "resume"}:
                if engine.s["status"] not in ({"RUNNING"} if action == "pause" else {"PAUSED"}):
                    raise ValueError("invalid state transition")
                at = engine.s["clock"] or data["events"][0]["at"]
                event = {"id": "CONTROL-" + uuid4().hex, "at": at, "type": action.upper()}
                engine.process(event)
                c.execute("INSERT INTO event_ledger VALUES(?,?,?)", (eid, event["id"], json.dumps(event)))
            elif action == "advance":
                if engine.s["status"] in {"READY", "COMPLETED", "FAILED"}:
                    return self.get(eid)
                for event in data["events"][cursor:cursor + limit]:
                    engine.process(event)
                    c.execute("INSERT INTO event_ledger VALUES(?,?,?)", (eid, event["id"], json.dumps(event, ensure_ascii=False)))
                    cursor += 1
                    if engine.s["status"] in {"COMPLETED", "FAILED"}:
                        break
            else:
                raise ValueError("unsupported action")
            result = engine.result()
            c.execute("UPDATE experiments SET result_json=?,cursor=?,state=? WHERE experiment_id=?", (json.dumps(result, ensure_ascii=False), cursor, engine.s["status"], eid))
        return self.get(eid)

