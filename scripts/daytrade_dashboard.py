"""Read-only daily dashboard. Resolve latest each request, never symlink the DB."""
import argparse
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path

from research_dashboard.server import Handler, single_query_value
from research_dashboard.daytrade_bridge import (
    daytrade_state_path, list_daytrade_sessions, read_daytrade_report, read_daytrade_session,
)


class ReadOnlyHandler(Handler):
    routes = ("/daytrade",)
    legacy_paths = {"/daytrade", "/daytrade.js", "/style.css"}
    read_only = True

    def do_POST(self):
        return self.reject_post('READ_ONLY_DASHBOARD')

    def get_api(self, path, query=""):
        if path == '/health':
            return self.response(200, {'status': 'ok', 'orders_enabled': False, 'read_only': True})
        if path not in {'/api/daytrade/report', '/api/daytrade/sessions'}:
            return self.response(404, {'error': 'NOT_FOUND'})
        if not self.authorized():
            return self.response(401, {'error': 'LOCAL_SESSION_REQUIRED'})
        root = self.server.root
        state_root = getattr(self.server, "state_root", None)
        if path == '/api/daytrade/sessions':
            return self.response(200, list_daytrade_sessions(root, state_root))
        try:
            day = single_query_value(query, "date")
        except ValueError:
            return self.response(400, {'error': 'INVALID_INPUT_OR_STATE'})
        if day is not None:
            result = read_daytrade_session(root, day, state_root)
            status = 400 if result.get("reason") == "DAYTRADE_DATE_INVALID" else 200
            return self.response(status, result)
        try:
            result = read_daytrade_report((daytrade_state_path(root, state_root) / 'latest').resolve(strict=True))
            return self.response(200, result)
        except Exception:
            return self.response(503, {'error': 'DAYTRADE_REPORT_UNAVAILABLE'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--state-root', required=True)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), ReadOnlyHandler)
    server.root, server.state_root = Path(args.root), Path(args.state_root)
    server.sessions, server.daytrade_lock = set(), threading.Lock()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__': main()
