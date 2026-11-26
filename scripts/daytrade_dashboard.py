"""Read-only daily dashboard. Resolve latest each request, never symlink the DB."""
import argparse
from http.server import ThreadingHTTPServer
import threading
from pathlib import Path

from research_dashboard.server import Handler
from research_dashboard.daytrade_bridge import read_daytrade_report


class ReadOnlyHandler(Handler):
    routes = ("/daytrade",)
    legacy_paths = {"/daytrade", "/daytrade.js", "/style.css"}
    read_only = True

    def do_POST(self):
        return self.reject_post('READ_ONLY_DASHBOARD')

    def get_api(self, path):
        if path == '/health':
            return self.response(200, {'status': 'ok', 'orders_enabled': False, 'read_only': True})
        if path != '/api/daytrade/report':
            return self.response(404, {'error': 'NOT_FOUND'})
        if not self.authorized():
            return self.response(401, {'error': 'LOCAL_SESSION_REQUIRED'})
        try:
            result = read_daytrade_report((self.server.state_root/'latest').resolve(strict=True))
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
