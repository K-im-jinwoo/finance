from __future__ import annotations

import json
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .notifications import drafts
from .rules import Policy
from .store import Store
from .live_accounts import LiveAccounts
from .oracle_bridge import HermesLiveWorker

class DashboardServer(ThreadingHTTPServer):
    def __init__(self, root: Path, port=8765, hermes_live=False, direct_market=False, allow_open_fills=False):
        self.root = root
        self.store = Store(root / ".runtime/paper.sqlite3")
        self.sessions = set()
        self.stopping = threading.Event()
        self.runner_errors = {}
        self.accounts = LiveAccounts(root / ".runtime/hermes-paper.sqlite3")
        self.live_worker = HermesLiveWorker(root,self.accounts,self.stopping,direct_market,allow_open_fills) if hermes_live else None
        super().__init__(("127.0.0.1", port), Handler)

    def run_worker(self):
        # Local synthetic replay only. Live observed data never gets an implicit runner.
        while not self.stopping.wait(0.8):
            for item in self.store.list():
                if item["state"] in {"RUNNING", "PAUSED"} and item["mode"] == "SYNTHETIC":
                    result = self.store.get(item["experiment_id"])
                    if result["cursor"] < result["total_events"]:
                        try:
                            self.store.apply(item["experiment_id"])
                        except Exception as exc:
                            # Keep error visible and stop advancing this local experiment.
                            self.runner_errors[item["experiment_id"]] = type(exc).__name__
                            with self.store.connect() as c:
                                row = c.execute("SELECT result_json FROM experiments WHERE experiment_id=?", (item["experiment_id"],)).fetchone()
                                state = json.loads(row["result_json"])
                                state["state"]["status"] = "FAILED"
                                state["state"]["outbox"].append({"event_id":"RUNNER-ERROR", "kind":"ERROR", "symbol":None, "at":state["state"]["clock"], "detail":"local calculation failed", "status":"DRAFT_ONLY", "source_event_id":"RUNNER-ERROR"})
                                c.execute("UPDATE experiments SET state='FAILED',result_json=? WHERE experiment_id=?", (json.dumps(state),item["experiment_id"]))

class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def response(self, status, payload, content_type="application/json; charset=utf-8", cookie=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"paper_session={cookie}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return "paper_session" in cookie and cookie["paper_session"].value in self.server.sessions
        except Exception:
            return False

    def do_GET(self):
        if not self.allowed_host() or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.response(403, {"error":"LOCAL_ORIGIN_REQUIRED"})
        path = urlsplit(self.path).path
        if path in {"/", "/hermes", "/app.js", "/live.js", "/style.css"}:
            files = {"/":("index.html","text/html; charset=utf-8"),"/hermes":("hermes.html","text/html; charset=utf-8"),"/app.js":("app.js","text/javascript; charset=utf-8"),"/live.js":("live.js","text/javascript; charset=utf-8"),"/style.css":("style.css","text/css; charset=utf-8")}
            name, kind = files[path]
            session = None
            if path in {"/","/hermes"} and not self.authorized():
                session = secrets.token_urlsafe(32)
                self.server.sessions.add(session)
            return self.response(200, (self.server.root / "web" / name).read_bytes(), kind, session)
        if path == "/health":
            return self.response(200, {"status":"ok","orders_enabled":False,"telegram_delivery_enabled":False,"bind":"loopback"})
        if not self.authorized():
            return self.response(401,{"error":"LOCAL_SESSION_REQUIRED"})
        try:
            if path == "/api/paper-accounts":
                return self.response(200,{"accounts":self.server.accounts.account_ids(),"source":self.server.live_worker.status if self.server.live_worker else {"state":"DISCONNECTED","mode":"NOT_STARTED"}})
            if path == "/api/hermes/reports":
                return self.response(200,self.server.accounts.reports())
            account_match = re.fullmatch(r"/api/paper-accounts/(P-[a-f0-9]{24})",path)
            if account_match:
                return self.response(200,self.server.accounts.get_account(account_match[1]))
            if path == "/api/status":
                audit = json.loads((self.server.root / "artifacts/source-audit.json").read_text(encoding="utf-8"))
                oracle_path = self.server.root / "artifacts/oracle-data-coverage.json"
                oracle = json.loads(oracle_path.read_text(encoding="utf-8-sig")) if oracle_path.exists() else None
                calculation_path = self.server.root / "artifacts/observed-price-calculation.json"
                calculation = json.loads(calculation_path.read_text(encoding="utf-8")) if calculation_path.exists() else None
                financial_path = self.server.root / "artifacts/observed-financial-calculation.json"
                financial = json.loads(financial_path.read_text(encoding="utf-8")) if financial_path.exists() else None
                return self.response(200,{"checked_at_kst":oracle["checked_at_kst"] if oracle else audit["checked_at_kst"],"source_head":audit["source_head"],"hashes_match":audit["all_snapshot_hashes_match"],"data_status":"THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY" if oracle else audit["real_three_year_replay"],"production_db_accessed":oracle is not None,"production_db_changed":False,"oracle_inventory":oracle,"actual_price_validation":calculation,"actual_financial_validation":financial,"runner_errors":self.server.runner_errors})
            if path == "/api/experiments":
                return self.response(200,self.server.store.list())
            match = re.fullmatch(r"/api/experiments/(E-[a-f0-9]{24})(/drafts)?",path)
            if match:
                result = self.server.store.get(match[1])
                return self.response(200,drafts(result) if match[2] else result)
            return self.response(404,{"error":"NOT_FOUND"})
        except KeyError:
            return self.response(404,{"error":"NOT_FOUND"})

    def do_POST(self):
        port = self.server.server_port
        origins = {f"http://127.0.0.1:{port}",f"http://localhost:{port}"}
        if not self.allowed_host() or not self.authorized() or self.headers.get("Origin") not in origins or self.headers.get("X-Paper-Action") != "1":
            # Consume only a bounded rejected body so Windows does not reset the
            # connection before delivering the 403 response. No parsing or writes.
            try:
                rejected_length = int(self.headers.get("Content-Length", "0"))
                if 0 <= rejected_length <= 8192:
                    self.rfile.read(rejected_length)
            except (ValueError, OSError):
                pass
            return self.response(403,{"error":"SESSION_AND_SAME_ORIGIN_REQUIRED"})
        try:
            length = int(self.headers.get("Content-Length","0"))
            if length < 0 or length > 8192:
                return self.response(413,{"error":"BODY_TOO_LARGE"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body,dict):
                raise ValueError("object required")
            path = urlsplit(self.path).path
            account_match = re.fullmatch(r"/api/paper-accounts/(P-[a-f0-9]{24})/(pause|resume)",path)
            if account_match:
                if body:
                    raise ValueError("control takes no settings")
                return self.response(200,self.server.accounts.control(account_match[1],account_match[2]))
            if path == "/api/experiments":
                if set(body) - {"cost_bps"}:
                    raise ValueError("only cost sensitivity supported")
                data = json.loads((self.server.root / "datasets/validation.json").read_text(encoding="utf-8"))
                eid = self.server.store.create(data,Policy(round_trip_bps=str(body.get("cost_bps","30"))))
                return self.response(201,{"experiment_id":eid})
            match = re.fullmatch(r"/api/experiments/(E-[a-f0-9]{24})/(start|pause|resume)",path)
            if match:
                item = self.server.store.get(match[1])
                if item["coverage"]["mode"] != "SYNTHETIC":
                    return self.response(409,{"error":"OBSERVED_RUN_REQUIRES_DATA_GATE_REVIEW"})
                return self.response(200,self.server.store.apply(match[1],match[2]))
            return self.response(404,{"error":"NOT_FOUND"})
        except KeyError:
            return self.response(404,{"error":"NOT_FOUND"})
        except (ValueError,TypeError):
            return self.response(400,{"error":"INVALID_INPUT_OR_STATE"})

