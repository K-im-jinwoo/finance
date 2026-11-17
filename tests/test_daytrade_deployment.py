"""Archive and dashboard scope validation, all filesystem fixtures/offline."""
import importlib.util
from contextlib import closing
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


def load_installer():
    spec = importlib.util.spec_from_file_location('daytrade_installer', ROOT/'engine/deploy/daytrade/install.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentTests(unittest.TestCase):
    def test_readonly_dashboard_uses_latest_daily_ledger_and_denies_post(self):
        from http.server import ThreadingHTTPServer
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
            (root/'engine/src/.env').write_text('secret')
            with closing(sqlite3.connect(root/'.runtime/intraday-paper.sqlite3')) as c:
                with c:
                    c.execute('CREATE TABLE test (value)')
                    c.execute('INSERT INTO test VALUES (42)')
            output = root/'release.tar.gz'
            manifest = make_bundle(root, output)
            self.assertEqual(set(manifest), {'web/daytrade.html'})
            with tarfile.open(output) as tar:
                self.assertIn('archive/2026-11-16.sqlite3', tar.getnames())
                self.assertNotIn('engine/src/.env', tar.getnames())
                self.assertEqual(json.load(tar.extractfile('release-manifest.json')), manifest)


if __name__ == '__main__': unittest.main()
