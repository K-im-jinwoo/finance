"""Append-only local prospective accounts, reusing the verified paper engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
from .contracts import digest, instant, number
from .engine import Engine
from .rules import Policy
from .store import Store


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def quote_view(quote, now, entry=None):
    result = {"price":None, "freshness":"MISSING", "source_timestamp":None, "observed_at":None,
              "source":None, "age_seconds":None, "pnl":None, "return":None, "gross_return":None}
    if not quote:
        return result
    try:
        price = number(quote["last_price"])
        age = (instant(now) - instant(quote["source_timestamp"])).total_seconds()
        if price <= 0 or age < 0 or quote["currency"] != "KRW" or instant(quote["observed_at"]) > instant(now) or instant(quote["observed_at"]) < instant(quote["source_timestamp"]):
            raise ValueError("invalid public price observation")
        result.update(price=str(price), source_timestamp=quote["source_timestamp"], observed_at=quote["observed_at"],
                      source=quote["source"], age_seconds=int(age), freshness="FRESH" if age <= 300 else "DELAYED" if age <= 900 else "STALE")
        # Stale/delayed quotes stay visible, but never appear as current P&L.
        if entry and age <= 300 and instant(quote["source_timestamp"]) >= instant(entry["entry_at"]):
            value = price * entry["quantity"]
            result.update(pnl=str(value-number(entry["cost"])), return_value=str(value/number(entry["cost"])-1),
                          gross_return=str(price/number(entry["entry_price"])-1))
            result["return"] = result.pop("return_value")
    except (KeyError, ValueError, TypeError):
        result["freshness"] = "INVALID"
        result["price"] = None
    return result


class LiveAccounts(Store):
    def __init__(self, path: Path):
        super().__init__(path)
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    account_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    config_json TEXT NOT NULL, dataset_json TEXT NOT NULL, state_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_events (
                    account_id TEXT NOT NULL, event_id TEXT NOT NULL, event_json TEXT NOT NULL,
                    PRIMARY KEY(account_id,event_id)
                );
                CREATE TABLE IF NOT EXISTS hermes_reports (
                    report_key TEXT PRIMARY KEY, published_at TEXT NOT NULL, report_json TEXT NOT NULL
                );
            ''')

    def create_account(self, now=None):
        now = now or utc_now()
        instant(now)
        aid = "P-" + uuid4().hex[:24]
        policy = Policy()
        data = {"schema_version":1,"mode":"OBSERVED","securities":{},"events":[],
                "limitations":["prospective Hermes top-1 only", "not a historical three-year result", "missing invalidation evidence remains UNKNOWN"]}
        engine = Engine(data, policy)
        engine.s.update(status="RUNNING",clock=now)
        meta = {"selection":"HERMES_CIO_EXPLICIT_RANK_1", "adopted_latest":False, "quotes":{}, "feed":{},
                "calendar":None, "last_open_session":None, "execution_gate":"VERIFIED_RAW_REGULAR_OPEN_REQUIRED"}
        with self.connect() as c:
            c.execute("INSERT INTO paper_accounts VALUES(?,?,?,?,?,?)", (aid,now,json.dumps(policy.snapshot()),json.dumps(data),json.dumps(engine.s),json.dumps(meta)))
        return aid

    def account_ids(self):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT account_id FROM paper_accounts ORDER BY created_at DESC")]

    def _load(self, row):
        policy = json.loads(row["config_json"])
        policy["proposals"] = tuple(policy["proposals"])
        engine = Engine(json.loads(row["dataset_json"]), Policy(**policy))
        engine.s = json.loads(row["state_json"])
        return engine, json.loads(row["meta_json"])

    def get_account(self, aid, now=None):
        now = now or utc_now()
        with self.connect() as c:
            row = c.execute("SELECT * FROM paper_accounts WHERE account_id=?",(aid,)).fetchone()
            if row is None:
                raise KeyError(aid)
            engine, meta = self._load(row)
            reports = [json.loads(r[0]) for r in c.execute("SELECT report_json FROM hermes_reports ORDER BY published_at DESC")]
        views = {symbol:quote_view(meta["quotes"].get(symbol),now,pos) for symbol,pos in engine.s["positions"].items()}
        current_assets = number(engine.s["cash"])
        current_valid = all(q["pnl"] is not None for q in views.values())
        if current_valid:
            current_assets += sum(number(views[s]["price"])*p["quantity"] for s,p in engine.s["positions"].items())
        result = engine.result()
        result.update(account_id=aid, created_at=row["created_at"], as_of=now, meta=meta, reports=reports, securities=engine.dataset["securities"],
                      position_quotes=views, watch_quotes={symbol:quote_view(q,now) for symbol,q in meta["quotes"].items()},
                      current_assets=str(current_assets) if current_valid else None,
                      current_return=str(current_assets/number(engine.policy.initial_cash)-1) if current_valid else None)
        return result

    def reports(self):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT report_json FROM hermes_reports ORDER BY published_at DESC")]

    def save_reports(self, reports):
        with self.connect() as c:
            for report in reports:
                old = c.execute("SELECT report_json FROM hermes_reports WHERE report_key=?",(report["key"],)).fetchone()
                if old and json.loads(old[0])["content_sha256"] != report["content_sha256"]:
                    raise ValueError("immutable Hermes report mismatch")
                c.execute("INSERT OR IGNORE INTO hermes_reports VALUES(?,?,?)",(report["key"],report["published_at"],json.dumps(report,ensure_ascii=False)))

    def _event(self, c, aid, engine, event):
        engine.process(event)
        c.execute("INSERT OR IGNORE INTO paper_events VALUES(?,?,?)",(aid,event["id"],json.dumps(event,ensure_ascii=False)))

    def update(self, aid, reports=(), quotes=(), feed=None, calendar=None, opened=None, now=None):
        now = now or utc_now()
        self.save_reports(reports)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM paper_accounts WHERE account_id=?",(aid,)).fetchone()
            if row is None:
                raise KeyError(aid)
            engine, meta = self._load(row)
            if instant(now) < instant(engine.s["clock"]):
                raise ValueError("past live observation")
            if feed is not None:
                meta["feed"] = feed
            if calendar is not None:
                meta["calendar"] = calendar
            # Only latest newly published report may generate a signal. The older finals are archive only.
            latest = max(reports,key=lambda r:(instant(r["published_at"]),r["message_id"]),default=None)
            if latest and latest["key"] not in engine.s["processed"]:
                eligible = latest["selected"] is not None
                if meta.get("last_report_published") and instant(latest["published_at"]) <= instant(meta["last_report_published"]):
                    eligible = False
                if engine.s["resume_at"] and instant(latest["published_at"]) <= instant(engine.s["resume_at"]):
                    eligible = False
                for candidate in latest["candidates"]:
                    engine.dataset["securities"].setdefault(candidate["symbol"], {"kind":"UNKNOWN","name":candidate["name"]})
                self._event(c,aid,engine,{"id":latest["key"],"type":"REPORT","at":now,"observed_at":now,
                    "published_at":latest["published_at"],"source":latest["source"],"report_id":latest["report_id"] or latest["key"],
                    "input_available_at":now,"candidates":[latest["selected"]] if eligible else [], "hermes_report_key":latest["key"]})
                meta.update(adopted_latest=True,last_report_published=latest["published_at"],latest_report_key=latest["key"])
            for quote in quotes:
                symbol = quote["symbol"]
                if symbol in engine.dataset["securities"] and quote_view(quote,now)["price"] is not None:
                    previous = meta["quotes"].get(symbol)
                    if previous is None or instant(quote["source_timestamp"]) >= instant(previous["source_timestamp"]):
                        meta["quotes"][symbol] = quote
            # Evidence gaps stay explicit; a quote or a CIO narrative cannot certify SMA/financial periods.
            symbols = set(engine.s["positions"]) | set(engine.s["pending"])
            for symbol in sorted(symbols):
                if symbol not in engine.s["checks"]:
                    self._event(c,aid,engine,{"id":"CHECK-"+uuid4().hex,"type":"CHECK","symbol":symbol,"at":now,"observed_at":now,"source":"LIVE_EVIDENCE_COVERAGE"})
            if opened is not None:
                self._apply_open(c,aid,engine,meta,opened,now)
            views = {s:quote_view(meta["quotes"].get(s),now) for s in engine.dataset["securities"]}
            self._event(c,aid,engine,{"id":"MARK-"+uuid4().hex,"type":"MARK","at":now,"observed_at":now,"source":"PUBLIC_QUOTE_OVERLAY",
                "prices":{s:q["price"] for s,q in views.items() if q["price"] is not None},
                "stale_symbols":[s for s,q in views.items() if q["freshness"] != "FRESH"]})
            self._save(c,aid,engine,meta)
        return self.get_account(aid,now)

    def _apply_open(self,c,aid,engine,meta,opened,now):
        # Provider adapter must use a complete unadjusted 1-minute bar ending open+1m,
        # an official regular-session calendar, and an observation within 5 minutes.
        start = instant(opened["open_at"])
        lag = (instant(now)-start).total_seconds()
        session = opened["session_id"]
        if session == meta["last_open_session"] or engine.s["status"] != "RUNNING":
            return
        if not 60 <= lag <= 300 or opened.get("basis") != "UNADJUSTED" or instant(opened["bar_end"]) != start+timedelta(minutes=1):
            raise ValueError("complete timely unadjusted regular open required")
        if not meta["calendar"] or not meta["calendar"].get("regular_open") or instant(meta["calendar"]["regular_open"]) != start:
            raise ValueError("opening calendar does not match")
        if instant(meta["calendar"]["observed_at"]) > start or instant(meta.get("worker_started_at",now)) >= start:
            raise ValueError("worker/calendar must be observed before this open")
        if any(number(v) <= 0 for v in opened.get("volumes",{}).values()) or set(opened["prices"]) != set(opened.get("volumes",{})):
            raise ValueError("actual opening trades required")
        required = set(engine.s["positions"]) | set(engine.s["pending"])
        if not required or not required.issubset(opened["prices"]):
            return
        self._event(c,aid,engine,{"id":"OPEN-"+session,"type":"OPEN","at":now,"observed_at":opened["observed_at"],"source":opened["source"],
            "session_id":session,"calendar_source":meta["calendar"]["source"],"calendar_available_at":meta["calendar"]["observed_at"],
            "open_at":opened["open_at"],"prices":opened["prices"],"basis":"UNADJUSTED", "bar_end":opened["bar_end"]})
        meta["last_open_session"] = session
        meta["execution_gate"] = "VERIFIED_OPEN_PROCESSED"

    def _save(self,c,aid,engine,meta):
        c.execute("UPDATE paper_accounts SET dataset_json=?,state_json=?,meta_json=? WHERE account_id=?",
                  (json.dumps(engine.dataset,ensure_ascii=False),json.dumps(engine.s,ensure_ascii=False),json.dumps(meta,ensure_ascii=False),aid))

    def control(self,aid,action,now=None):
        now = now or utc_now()
        if action not in {"pause","resume"}:
            raise ValueError("unsupported control")
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM paper_accounts WHERE account_id=?",(aid,)).fetchone()
            if row is None:
                raise KeyError(aid)
            engine, meta = self._load(row)
            if engine.s["status"] != ("RUNNING" if action == "pause" else "PAUSED"):
                raise ValueError("invalid account transition")
            self._event(c,aid,engine,{"id":"CONTROL-"+uuid4().hex,"type":action.upper(),"at":now})
            self._save(c,aid,engine,meta)
        return self.get_account(aid,now)

    def worker_started(self,now=None):
        now = now or utc_now()
        with self.connect() as c:
            for row in c.execute("SELECT * FROM paper_accounts").fetchall():
                engine, meta = self._load(row)
                meta["worker_started_at"] = now
                self._save(c,row["account_id"],engine,meta)
