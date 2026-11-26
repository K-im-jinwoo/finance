"""Archive and dashboard scope validation, all filesystem fixtures/offline."""
import importlib.util
from contextlib import closing
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import threading
import urllib.request
import urllib.error
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'src'))


def load_installer():
    spec = importlib.util.spec_from_file_location('daytrade_installer', ROOT/'engine/deploy/daytrade/install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentTests(unittest.TestCase):
    def test_readonly_spa_capabilities_route_scope_and_legacy_fallback(self):
        from daytrade_dashboard import ReadOnlyHandler
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            assets = root / "web/dist/assets"
            assets.mkdir(parents=True)
            index = assets.parent / "index.html"
            index.write_bytes(b"React fixture")
            (assets / "bundle.js").write_bytes(b"dashboard fixture")
            (root / "web/daytrade.html").write_bytes(b"legacy fixture")
            server = ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyHandler)
            server.root, server.sessions = root, set()
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/daytrade?date=2026-11-17") as response:
                    self.assertEqual(response.read(), b"React fixture")
                    raw_cookie = response.headers["Set-Cookie"]
                    self.assertIn("HttpOnly; SameSite=Strict; Path=/", raw_cookie)
                    self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
                with urllib.request.urlopen(base + "/assets/bundle.js") as response:
                    self.assertEqual(response.read(), b"dashboard fixture")
                    self.assertEqual(response.headers["Content-Type"], "text/javascript; charset=utf-8")
                    self.assertIsNone(response.headers.get("Set-Cookie"))
                headers = {"Cookie": raw_cookie.split(";")[0]}
                request = urllib.request.Request(base + "/api/capabilities", headers=headers)
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response), {
                        "read_only": True,
                        "orders_enabled": False,
                        "telegram_delivery_enabled": False,
                        "routes": ["/daytrade"],
                        "phase_b_available": False,
                    })
                for route in ("/", "/hermes", "/experiments", "/research", "/swing", "/ops", "/api/paper-accounts"):
                    with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(base + route, headers=headers))
                    self.assertEqual(error.exception.code, 404)
                    error.exception.close()
                for route, request_headers, code in (("/api/capabilities", {}, 401),
                                                        ("/assets/bundle.js", {"Host": "example.invalid"}, 403),
                                                        ("/daytrade", {"Sec-Fetch-Site": "cross-site"}, 403)):
                    with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(base + route, headers=request_headers))
                    self.assertEqual(error.exception.code, code)
                    error.exception.close()
                index.unlink()
                with urllib.request.urlopen(base + "/daytrade") as response:
                    self.assertEqual(response.read(), b"legacy fixture")
                self.assertFalse((root / ".runtime").exists())
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()

    def test_readonly_serves_the_production_vite_document(self):
        from daytrade_dashboard import ReadOnlyHandler
        from test_daytrade_dashboard import PageSafety

        index = ROOT / "web/dist/index.html"
        if not index.is_file():
            self.skipTest("Build the Vite dashboard to verify its read-only route")
        with ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyHandler) as server:
            server.root, server.sessions = ROOT, set()
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(base + "/daytrade") as response:
                    document = response.read()
                    self.assertEqual(document, index.read_bytes())
                    self.assertIn("style-src 'self'", response.headers["Content-Security-Policy"])
                parser = PageSafety()
                parser.feed(document.decode())
                for url in parser.scripts | parser.stylesheets:
                    with urllib.request.urlopen(base + url) as response:
                        self.assertEqual(response.read(), (index.parent / url.lstrip("/")).read_bytes())
            finally:
                server.shutdown()
                worker.join(5)

    def test_readonly_dashboard_uses_latest_daily_ledger_and_denies_post(self):
        from daytrade_dashboard import ReadOnlyHandler
        from stock_assistant.daytrade.daemon import open_or_resume_session
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = open_or_resume_session(root/'state', '2026-11-17')
            session.on_session(normal_session_verified=True, at='2026-11-17T09:10:00+09:00')
            path = session.ledger.path
            session.close()
            before = path.read_bytes()
            # Directory fixture exercises the identical resolved-path read on Windows.
            server = ThreadingHTTPServer(('127.0.0.1', 0), ReadOnlyHandler)
            server.root = root
            server.state_root = root/'state'
            latest = root/'state/latest'
            if not latest.exists():
                import shutil
                shutil.copytree(path.parent.parent, latest)
            server.sessions = {'test-session'}
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/api/daytrade/report'
                request = urllib.request.Request(url, headers={'Cookie': 'paper_session=test-session'})
                with urllib.request.urlopen(request) as response:
                    result = json.load(response)
                self.assertTrue(result['report']['paper_ready'])
                self.assertFalse(result['report']['orders_enabled'])
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url, data=b'{}', method='POST'))
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
                self.assertEqual(before, path.read_bytes())
            finally:
                server.shutdown()
                worker.join(5)
                server.server_close()

    def test_release_tar_rejects_traversal_and_symlinks(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('safe', tarfile.SYMTYPE)):
            with tempfile.TemporaryDirectory() as root:
                archive = Path(root)/'invalid.tar'
                with tarfile.open(archive, 'w') as tar:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    tar.addfile(info)
                with self.assertRaisesRegex(ValueError, 'UNSAFE_RELEASE_MEMBER'):
                    load_installer().extract(archive, Path(root)/'target')

    def test_bundle_excludes_runtime_secrets_and_contains_sqlite_backup(self):
        from oracle_daytrade_deploy import make_bundle
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for relative in ('engine/src', 'src', 'web', 'scripts', 'engine/deploy', '.runtime'):
                (root/relative).mkdir(parents=True, exist_ok=True)
            (root/'web/daytrade.html').write_text('dashboard')
            (root/'web/dist/assets').mkdir(parents=True)
            (root/'web/dist/index.html').write_text('React fixture')
            (root/'web/dist/assets/bundle.js').write_text('JS fixture')
            (root/'web/node_modules/package').mkdir(parents=True)
            (root/'web/node_modules/package/index.js').write_text('dependency fixture')
            (root/'engine/src/.env').write_text('secret')
            with closing(sqlite3.connect(root/'.runtime/intraday-paper.sqlite3')) as c:
                with c:
                    c.execute('CREATE TABLE test (value)')
                    c.execute('INSERT INTO test VALUES (42)')
            output = root/'release.tar.gz'
            manifest = make_bundle(root, output)
            self.assertEqual(set(manifest), {'web/daytrade.html', 'web/dist/index.html', 'web/dist/assets/bundle.js'})
            with tarfile.open(output) as tar:
                self.assertEqual(set(tar.getnames()), set(manifest) | {
                    'archive/2026-11-16.sqlite3', 'release-manifest.json',
                })
                self.assertEqual(json.load(tar.extractfile('release-manifest.json')), manifest)


if __name__ == '__main__': unittest.main()
