"""Explicitly invoked production deployment; tests never execute main()."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import tarfile

from oracle_unified_rollout import SSH, SCP, HOST, OPTIONS, call

ROOT = Path(__file__).resolve().parents[1]


def make_bundle(root, output):
    output = Path(output)
    files = []
    for relative in ('engine/src', 'src', 'web', 'scripts', 'engine/deploy'):
        files.extend(p for p in (root/relative).rglob('*') if p.is_file()
            and '__pycache__' not in p.parts and 'node_modules' not in p.relative_to(root).parts
            and p.suffix not in {'.pyc', '.sqlite3', '.db', '.log', '.pem', '.key'}
            and not p.name.startswith('.env'))
    manifest = {}
    snapshot = output.with_suffix('.sqlite3')
    source = (root/'.runtime/intraday-paper.sqlite3').resolve()
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as incoming, closing(sqlite3.connect(snapshot)) as target:
        incoming.backup(target)
    with tarfile.open(output, 'w:gz') as tar:
        for file in sorted(files):
            relative = file.relative_to(root).as_posix()
            manifest[relative] = hashlib.sha256(file.read_bytes()).hexdigest()
            tar.add(file, arcname=relative, recursive=False)
        tar.add(snapshot, arcname='archive/2026-11-16.sqlite3')
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo('release-manifest.json')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--deploy', action='store_true', required=True)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT/'.runtime/daytrade-oracle-service-deploy'
    out.mkdir(parents=True, exist_ok=True)
    archive = out/(stamp+'.tar.gz')
    manifest = make_bundle(ROOT, archive)
    (out/'release-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    remote = '/tmp/daytrade-release-'+stamp+'.tar.gz'
    call([SCP, *OPTIONS, str(archive), HOST+':'+remote], 120)
    # Only create the new service root with sudo; all install/runtime work is ubuntu.
    call([SSH, '-T', *OPTIONS, HOST, 'sudo -n install -d -o ubuntu -g ubuntu -m 0750 /srv/daytrade'], 30)
    code = "import tarfile,sys;t=tarfile.open(sys.argv[1]);exec(compile(t.extractfile('engine/deploy/daytrade/install.py').read(),'daytrade-install','exec'))"
    result = call([SSH, '-T', *OPTIONS, HOST, shlex.join(['python3', '-c', code, remote, stamp])], 180)
    (out/'deployment.json').write_text(result, encoding='utf-8')
    print(json.dumps({'release_id': stamp, 'deployment_result': str(out/'deployment.json')}))


if __name__ == '__main__': main()
