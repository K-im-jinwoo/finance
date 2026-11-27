"""Serve the integrated dashboard over loopback using read-only data sources."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import threading

from bootstrap import ROOT
from research_dashboard.daytrade_bridge import daytrade_state_path, read_daytrade_report
from research_dashboard.longterm_ledger import LongTermAccounts
from research_dashboard.server import Handler


HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def checked_host(value):
    if value is None:
        return None
    value = value.lower()
    name, colon, port = value.partition(":")
    if (len(name) > 253 or not HOST.fullmatch(name)
            or colon and (not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535)):
        raise ValueError("allowed host must be a hostname with an optional port")
    return value


class OracleHandler(Handler):
    routes = ("/", "/longterm", "/daytrade", "/research", "/swing", "/ops")
    legacy_paths = ()
    read_only = True
    phase_b_available = True

    def allowed_host(self):
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and (super().allowed_host() or hosts[0].lower() == self.server.allowed_host)

    def static_response(self, file, kind, issue_session=False):
        # A server-lifetime cookie avoids accumulating in-memory sessions on this read-only server.
        cookie = self.server.read_session if issue_session and not self.authorized() else None
        return self.response(200, file.read_bytes(), kind, cookie)

    def do_POST(self):
        return self.reject_post("READ_ONLY_DASHBOARD")

    def latest_daytrade_report(self):
        latest = self.server.daytrade_latest
        if not latest.is_dir():
            return {"present": False, "report": None, "reason": "DAYTRADE_SESSION_UNAVAILABLE"}
        return read_daytrade_report(latest.resolve(strict=True), dated=True)


class OracleDashboardServer(ThreadingHTTPServer):
    def __init__(self, root, port=8765, *, allowed_host=None):
        self.root = Path(root)
        self.allowed_host = checked_host(allowed_host)
        self.daytrade_latest = daytrade_state_path(self.root) / "latest"
        self.longterm = LongTermAccounts(self.root)
        self.read_session = secrets.token_urlsafe(32)
        self.sessions = {self.read_session}
        self.runner_errors = {}
        self.daytrade_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), OracleHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allowed-host", help="Exact additional Host header for the private HTTPS proxy")
    args = parser.parse_args()
    if not (args.root / "web/dist/index.html").is_file():
        parser.error("build dashboard first: npm --prefix dashboard run build")
    try:
        server = OracleDashboardServer(args.root, args.port, allowed_host=args.allowed_host)
    except ValueError as exc:
        parser.error(str(exc))
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
