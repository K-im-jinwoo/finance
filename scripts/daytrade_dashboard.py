"""Read-only daily dashboard. Resolve latest each request, never symlink the DB."""
import argparse
from http.server import ThreadingHTTPServer
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from research_dashboard.server import Handler
from research_dashboard.daytrade_bridge import read_daytrade_report


class ReadOnlyHandler(Handler):
    def do_POST(self):
        self.response(403, {'error': 'READ_ONLY_DASHBOARD'})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {'/api/daytrade/report', '/health'}:
            if not self.allowed_host() or self.headers.get('Sec-Fetch-Site') == 'cross-site':
                return self.response(403, {'error': 'LOCAL_ORIGIN_REQUIRED'})
            if path == '/health':
                return self.response(200, {'status': 'ok', 'orders_enabled': False, 'read_only': True})
            if not self.authorized():
                return self.response(401, {'error': 'LOCAL_SESSION_REQUIRED'})
            try:
                result = read_daytrade_report((self.server.state_root/'latest').resolve(strict=True))
                return self.response(200, result)
            except Exception:
                return self.response(503, {'error': 'DAYTRADE_REPORT_UNAVAILABLE'})
        if path not in {'/daytrade', '/daytrade.js', '/style.css'}:
            return self.response(404, {'error': 'NOT_FOUND'})
        super().do_GET()


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
