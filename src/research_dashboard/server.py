from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .notifications import drafts
from .rules import Policy
from .store import Store
from .live_accounts import LiveAccounts
from .longterm_ledger import LongTermAccounts
from .phase_b_db import DB_ERRORS
from .oracle_bridge import HermesLiveWorker
from .daytrade_bridge import list_daytrade_sessions, read_daytrade_report, read_daytrade_session, simulate_sample_daytrade
from .ops_bridge import get_ops_overview
from .research_bridge import get_research_chart, get_research_report, list_research_reports
from .swing_bridge import get_performance, get_swing_report, list_swing_reports

HTML_TYPE = "text/html; charset=utf-8"
ASSET_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}
LEGACY_FILES = {
    "/": ("index.html", HTML_TYPE),
    "/hermes": ("hermes.html", HTML_TYPE),
    "/daytrade": ("daytrade.html", HTML_TYPE),
    "/app.js": ("app.js", ASSET_TYPES[".js"]),
    "/live.js": ("live.js", ASSET_TYPES[".js"]),
    "/daytrade.js": ("daytrade.js", ASSET_TYPES[".js"]),
    "/style.css": ("style.css", ASSET_TYPES[".css"]),
}
LEGACY_SESSION_PATHS = {"/", "/hermes", "/daytrade", "/daytrade.js"}
MAX_POST_BYTES = 8192
LONGTERM_READS = {"/api/longterm/overview": "overview", "/api/longterm/account": "account"}
LONGTERM_ACTIONS = {"/api/longterm/account/init": "initialize", "/api/longterm/refresh": "refresh",
                    "/api/longterm/account/pause": "pause", "/api/longterm/account/resume": "resume"}

def dashboard_status(root, runner_errors=None):
    artifacts = root / "artifacts"
    def read_artifact(name):
        value = json.loads((artifacts / name).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("verification artifact must be an object")
        return value

    def optional(name):
        try:
            return read_artifact(name)
        except FileNotFoundError:
            return None

    audit = read_artifact("source-audit.json")
    oracle = optional("oracle-data-coverage.json")
    synthetic = oracle is not None and oracle.get("mode") == "SYNTHETIC"
    return {
        **({"mode": "SYNTHETIC"} if synthetic else {}),
        "checked_at_kst": oracle["checked_at_kst"] if oracle else audit["checked_at_kst"],
        "source_head": audit["source_head"],
        "hashes_match": audit["all_snapshot_hashes_match"],
        "data_status": "THREE_YEAR_INCOMPLETE_ORACLE_INVENTORY" if oracle else audit["real_three_year_replay"],
        "production_db_accessed": oracle is not None and not synthetic,
        "production_db_changed": False,
        "oracle_inventory": oracle,
        "actual_price_validation": optional("observed-price-calculation.json"),
        "actual_financial_validation": optional("observed-financial-calculation.json"),
        "runner_errors": {} if runner_errors is None else runner_errors,
    }


def single_query_value(query, name):
    values = parse_qs(query, keep_blank_values=True, max_num_fields=16).get(name, [])
    if len(values) > 1 or values == [""]:
        raise ValueError("invalid query parameter")
    return values[0] if values else None


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, root: Path, port=8765, hermes_live=False, direct_market=False, allow_open_fills=False, *, synthetic=False):
        self.root = root
        self.longterm = LongTermAccounts(root, synthetic=synthetic)
        self.store = Store(root / ".runtime/paper.sqlite3")
        self.sessions = set()
        self.stopping = threading.Event()
        self.runner_errors = {}
        self.daytrade_lock = threading.Lock()
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
    routes = ("/", "/longterm", "/daytrade", "/experiments", "/research", "/swing", "/ops")
    legacy_paths = LEGACY_FILES.keys()
    read_only = False
    phase_b_available = None

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

    def local_request(self):
        return self.allowed_host() and self.headers.get("Sec-Fetch-Site") != "cross-site"

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return "paper_session" in cookie and cookie["paper_session"].value in self.server.sessions
        except Exception:
            return False

    def do_GET(self):
        if not self.local_request():
            return self.response(403, {"error":"LOCAL_ORIGIN_REQUIRED"})
        url = urlsplit(self.path)
        path = url.path
        if path == "/favicon.ico":
            return self.response(204, b"", "image/x-icon")
        if path in self.routes or (path == "/hermes" and "/longterm" in self.routes):
            index = self.built_file("index.html")
            if index is not None:
                return self.static_response(index, HTML_TYPE, issue_session=True)
        if path == "/assets" or path.startswith("/assets/"):
            return self.get_asset(path.removeprefix("/assets/"))
        if path in self.legacy_paths:
            name, kind = LEGACY_FILES[path]
            return self.static_response(self.server.root / "web" / name, kind, path in LEGACY_SESSION_PATHS)
        if path == "/api/capabilities":
            if not self.authorized():
                return self.response(401, {"error":"LOCAL_SESSION_REQUIRED"})
            return self.response(200, self.capabilities())
        return self.get_api(path, url.query)

    def capabilities(self):
        return {
            "read_only": self.read_only,
            "orders_enabled": False,
            "telegram_delivery_enabled": False,
            "routes": self.routes,
            "phase_b_available": self.phase_b_available if self.phase_b_available is not None else not self.read_only,
            "daytrade_sessions_available": True,
            **({"fixture_mode": "SYNTHETIC"} if getattr(self.server, "synthetic_audit", False) else {}),
        }

    def built_file(self, *parts: str) -> Path | None:
        try:
            # The deployment root can itself be the trusted current-release link.
            candidate = self.server.root.resolve().joinpath("web", "dist", *parts)
            if candidate.resolve(strict=True) == candidate and candidate.is_file():
                return candidate
        except (OSError, RuntimeError):
            return None
        return None

    def get_asset(self, name):
        # Vite emits flat filenames. Decode once before checking the name and suffix.
        name = unquote(name)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            kind = ASSET_TYPES.get(Path(name).suffix)
            if kind and self.built_file("index.html") is not None:
                asset = self.built_file("assets", name)
                if asset is not None:
                    return self.static_response(asset, kind)
        return self.response(404, {"error":"NOT_FOUND"})

    def static_response(self, file, kind, issue_session=False):
        content = file.read_bytes()
        session = None
        if issue_session and not self.authorized():
            session = secrets.token_urlsafe(32)
            self.server.sessions.add(session)
        return self.response(200, content, kind, session)

    def phase_b_data(self, path, query):
        root = self.server.root
        if path == "/api/research/reports":
            return list_research_reports(root)
        match = re.fullmatch(r"/api/research/reports/([^/]+)", path)
        if match:
            return get_research_report(root, match[1])
        match = re.fullmatch(r"/api/research/chart/([0-9]{6})", path)
        if match:
            report_id = single_query_value(query, "report_id")
            if report_id is None:
                raise ValueError("report_id is required")
            return get_research_chart(root, match[1], report_id)
        if path == "/api/swing/reports":
            return list_swing_reports(root)
        match = re.fullmatch(r"/api/swing/reports/([^/]+)", path)
        if match:
            return get_swing_report(root, match[1])
        if path == "/api/performance":
            return get_performance(root, single_query_value(query, "report_id"))
        if path == "/api/ops/overview":
            try:
                status = dashboard_status(root, self.server.runner_errors)
            except (OSError, TypeError, ValueError, KeyError):
                status = {"present": False, "reason": "VERIFICATION_UNAVAILABLE"}
            return get_ops_overview(root, status)
        if path == "/api/daytrade/sessions":
            return list_daytrade_sessions(root)
        if path == "/api/daytrade/report":
            date = single_query_value(query, "date")
            if date is not None:
                return read_daytrade_session(root, date)
        return None

    def latest_daytrade_report(self):
        return read_daytrade_report(self.server.root)

    def get_api(self, path, query=""):
        if path == "/health":
            return self.response(200, {"status":"ok","orders_enabled":False,"telegram_delivery_enabled":False,"bind":"loopback"})
        if not self.authorized():
            return self.response(401,{"error":"LOCAL_SESSION_REQUIRED"})
        if action := LONGTERM_READS.get(path):
            try:
                return self.response(200, getattr(self.server.longterm, action)())
            except DB_ERRORS:
                return self.response(503, {"error": "LONGTERM_LEDGER_UNAVAILABLE"})
        try:
            data = self.phase_b_data(path, query)
        except ValueError:
            return self.response(400, {"error": "INVALID_INPUT_OR_STATE"})
        if data is not None:
            invalid = data.get("reason") in {"INVALID_REPORT_ID", "INVALID_SYMBOL", "DAYTRADE_DATE_INVALID"}
            return self.response(400 if invalid else 200, data)
        try:
            if path == "/api/daytrade/report":
                try:
                    with self.server.daytrade_lock:
                        result = self.latest_daytrade_report()
                    return self.response(200, result)
                except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                    return self.response(503, {"error":"DAYTRADE_REPORT_UNAVAILABLE"})
            if self.read_only:
                return self.response(404, {"error": "NOT_FOUND"})
            if path == "/api/paper-accounts":
                return self.response(200,{"accounts":self.server.accounts.account_ids(),"source":self.server.live_worker.status if self.server.live_worker else {"state":"DISCONNECTED","mode":"NOT_STARTED"}})
            if path == "/api/hermes/reports":
                return self.response(200,self.server.accounts.reports())
            account_match = re.fullmatch(r"/api/paper-accounts/(P-[a-f0-9]{24})",path)
            if account_match:
                return self.response(200,self.server.accounts.get_account(account_match[1]))
            if path == "/api/status":
                return self.response(200, dashboard_status(self.server.root, self.server.runner_errors))
            if path == "/api/experiments":
                return self.response(200,self.server.store.list())
            match = re.fullmatch(r"/api/experiments/(E-[a-f0-9]{24})(/drafts)?",path)
            if match:
                result = self.server.store.get(match[1])
                return self.response(200,drafts(result) if match[2] else result)
            return self.response(404,{"error":"NOT_FOUND"})
        except KeyError:
            return self.response(404,{"error":"NOT_FOUND"})

    def reject_post(self, error):
        # Consume only a bounded rejected body so Windows delivers the response.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 <= length <= MAX_POST_BYTES:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        return self.response(403, {"error": error})

    def do_POST(self):
        origin = f"http://{self.headers.get('Host')}"
        if (not self.local_request() or not self.authorized() or self.headers.get("Origin") != origin
                or self.headers.get("X-Paper-Action") != "1"):
            return self.reject_post("SESSION_AND_SAME_ORIGIN_REQUIRED")
        try:
            length = int(self.headers.get("Content-Length","0"))
            if length < 0 or length > MAX_POST_BYTES:
                return self.response(413,{"error":"BODY_TOO_LARGE"})
            body = json.loads(self.rfile.read(length))
            if not isinstance(body,dict):
                raise ValueError("object required")
            path = urlsplit(self.path).path
            if action := LONGTERM_ACTIONS.get(path):
                if body:
                    raise ValueError("long-term action takes no settings")
                try:
                    account = self.server.longterm
                    result = account.control(action) if action in {"pause", "resume"} else getattr(account, action)()
                except (OSError, sqlite3.Error):
                    return self.response(503, {"error": "LONGTERM_SOURCE_UNAVAILABLE"})
                return self.response(200, result)
            if path == "/api/daytrade/simulate-sample":
                if body:
                    raise ValueError("sample action requires an explicit empty object")
                try:
                    with self.server.daytrade_lock:
                        result = simulate_sample_daytrade(self.server.root)
                    return self.response(201, result)
                except FileExistsError:
                    return self.response(409, {"error":"DAYTRADE_LEDGER_EXISTS"})
                except FileNotFoundError:
                    return self.response(404, {"error":"DAYTRADE_SAMPLE_UNAVAILABLE"})
                except (OSError, sqlite3.Error):
                    return self.response(503, {"error":"DAYTRADE_SIMULATION_UNAVAILABLE"})
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

