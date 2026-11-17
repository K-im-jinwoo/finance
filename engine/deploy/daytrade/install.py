"""Ubuntu-side installer. Mutates /srv/daytrade and the three daytrade user units only."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile

BASE = Path('/srv/daytrade')
PROTECTED = ('stock-assistant-stock-assistant-1', 'stock-dashboard-market-1', 'deploy-wiki-agent-1')


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def inventory():
    containers = {}
    for name in PROTECTED:
        obj = json.loads(command('docker', 'inspect', name))[0]
        containers[name] = {'id': obj['Id'], 'pid': obj['State']['Pid'], 'started_at': obj['State']['StartedAt'],
                            'restart_count': obj['RestartCount'], 'running': obj['State']['Running']}
    files = [Path('/srv/stock-dashboard/shared-auth-cache/toss-token.json'),
             Path('/srv/stock-dashboard/shared-auth-cache/toss-token.lock'),
             Path('/home/ubuntu/.hermes/profiles/stock-cio/.env'),
             Path('/home/ubuntu/.hermes/profiles/stock-cio/config.yaml')]
    return {'containers': containers, 'file_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            'gateway': command('systemctl', '--user', 'show', 'hermes-gateway-stock-cio.service',
                              '-p', 'MainPID', '-p', 'ActiveEnterTimestampMonotonic'),
            'linger': command('loginctl', 'show-user', 'ubuntu', '-p', 'Linger')}


def extract(archive, destination):
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('UNSAFE_RELEASE_MEMBER')
        tar.extractall(destination, filter='data')


def switch(link, target):
    temporary = link.with_name(link.name + '.new')
    if temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def install(archive, stamp):
    if not stamp.isalnum():
        raise ValueError('INVALID_RELEASE_ID')
    if command('id', '-un') != 'ubuntu':
        raise ValueError('UBUNTU_USER_REQUIRED')
    before = inventory()
    if before['linger'] != 'Linger=yes':
        raise ValueError('LINGER_REQUIRED')
    release = BASE/'releases'/stamp
    release.mkdir(parents=True)  # Immutable: never overwrite an existing release.
    state = BASE/'state'
    state.mkdir(mode=0o700, exist_ok=True)
    extract(archive, release)
    manifest = json.loads((release/'release-manifest.json').read_text())
    for relative, expected in manifest.items():
        if hashlib.sha256((release/relative).read_bytes()).hexdigest() != expected:
            raise ValueError('RELEASE_HASH_MISMATCH')
    source = release/'archive/2026-11-16.sqlite3'
    previous = state/'daily/2026-11-16'
    (previous/'.runtime').mkdir(parents=True, exist_ok=True)
    target = previous/'.runtime/intraday-paper.sqlite3'
    with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as connection:
        value = json.loads(connection.execute('SELECT payload FROM state').fetchone()[0])
        if value['sessions'] != {'2026-11-16': 'CLOSED'} or value['positions'] or value['cash'] != '9995188.8384':
            raise ValueError('APPROVED_PREVIOUS_LEDGER_REQUIRED')
        if not target.exists():
            with sqlite3.connect(target) as output:
                connection.backup(output)
            target.chmod(0o600)
    python = '/home/ubuntu/.hermes/hermes-agent/venv/bin/python'
    command(python, '-c', 'import websockets.sync.client,dotenv')
    env = state/'service.env'
    env.write_text('PYTHONPATH=/srv/daytrade/current/engine/src:/srv/daytrade/current/src:/srv/daytrade/current/scripts\n'
        'PYTHONDONTWRITEBYTECODE=1\n'
        'DAYTRADE_TOKEN_CACHE=/srv/stock-dashboard/shared-auth-cache\n'
        'DAYTRADE_CLIENT_ID_FILE=/srv/stock-assistant/toss-client-id\n'
        'DAYTRADE_TELEGRAM_PROFILE=/home/ubuntu/.hermes/profiles/stock-cio\n', encoding='utf-8')
    env.chmod(0o600)
    units = Path.home()/'.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    backup = state/'deployment-backups'/stamp
    backup.mkdir(parents=True)
    current = BASE/'current'
    prior = str(current.resolve()) if current.is_symlink() else None
    for name in ('daytrade-paper.service', 'daytrade-paper.timer', 'daytrade-dashboard.service'):
        if (units/name).exists():
            shutil.copy2(units/name, backup/name)
        shutil.copy2(release/'engine/deploy/daytrade'/name, units/name)
    # No protected service/container command appears in the mutation path.
    for name in ('daytrade-paper.service', 'daytrade-dashboard.service'):
        if command('systemctl', '--user', 'show', name, '-p', 'LoadState', '--value') != 'not-found':
            command('systemctl', '--user', 'stop', name)
    switch(current, release)
    if not (state/'latest').exists():
        switch(state/'latest', previous)
    command('systemctl', '--user', 'daemon-reload')
    command('systemctl', '--user', 'enable', '--now', 'daytrade-paper.timer', 'daytrade-dashboard.service')
    command('systemctl', '--user', 'enable', 'daytrade-paper.service')
    command('systemctl', '--user', 'start', 'daytrade-paper.service')
    after = inventory()
    result = {'release_id': stamp, 'release': str(release), 'previous_release': prior,
              'before': before, 'after': after, 'protected_unchanged': before == after,
              'at_utc': datetime.now(timezone.utc).isoformat()}
    (state/'deployment.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__': install(sys.argv[1], sys.argv[2])
