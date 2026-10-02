"""Approved coordinated auth cutover. Preserve stopped containers for rollback.

Input/output contain public sources and deployment metadata only. Never execute
the collector, modify its DB, register keys, or send a test message.
"""
import base64
import copy
import hashlib
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/srv/stock-dashboard')
COLLECTOR = 'stock-assistant-stock-assistant-1'
GATEWAY = 'stock-dashboard-market-1'
IMAGE = 'sha256:ff00570ac14805bcccb878b4feed4c7757749f1849e8b3ca19faa3bedd037daa'
WRAPPER = Path('/srv/stock-assistant/releases/f3af37a-wip-20260928-intraday-dedup-v10/deploy/jobs/run_intraday_watch.sh')
WRAPPER_SHA = '25ca73279a0a61b3ac17654655f928396ef80341b5ea7802991bf9c549b0cb1e'
OLD_LAUNCHER = b'python -m stock_assistant refresh-intraday'
NEW_LAUNCHER = b'python /opt/stock-shared-auth/shared_collector_entry.py refresh-intraday'
FILES = {'market_gateway.py', 'market_read_actor.py', 'shared_toss_auth.py',
         'shared_gateway_entry.py', 'shared_collector_entry.py'}
CACHE = ROOT / 'shared-auth-cache'
CODE = ROOT / 'shared-auth-code'

def command(args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)

def inspect(name):
    return json.loads(command(['docker', 'inspect', name]))[0]

def decode_bundle(bundle):
    if set(bundle) != {'files', 'override'} or set(bundle['files']) != FILES:
        raise ValueError('UNEXPECTED_BUNDLE_FILES')
    result = {}
    for name, item in bundle['files'].items():
        raw = base64.b64decode(item['base64'], validate=True)
        if len(raw) > 100000 or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('SOURCE_HASH_MISMATCH')
        compile(raw, name, 'exec')
        result[name] = raw
    override = base64.b64decode(bundle['override']['base64'], validate=True)
    if hashlib.sha256(override).hexdigest() != bundle['override']['sha256'] or len(override) > 4096:
        raise ValueError('OVERRIDE_HASH_MISMATCH')
    return result, override

def collector_spec(old):
    # Docker Engine cloning preserves all existing limits, logging, mounts,
    # health check, arguments, labels and environment without printing them.
    config = copy.deepcopy(old['Config'])
    if any(v.startswith('STOCK_TOSS_AUTH_CACHE_DIR=') for v in config['Env']):
        raise ValueError('SHARED_PROFILE_ALREADY_PRESENT_RECHECK_REQUIRED')
    config['Env'].append('STOCK_TOSS_AUTH_CACHE_DIR=/run/stock-shared-auth')
    host = copy.deepcopy(old['HostConfig'])
    host.setdefault('Mounts', []).extend([
        {'Type': 'bind', 'Source': str(CODE), 'Target': '/opt/stock-shared-auth', 'ReadOnly': True},
        {'Type': 'bind', 'Source': str(CACHE), 'Target': '/run/stock-shared-auth', 'ReadOnly': False},
    ])
    networks = {name: {'Aliases': value.get('Aliases') or []}
                for name, value in old['NetworkSettings']['Networks'].items()}
    return {**config, 'HostConfig': host, 'NetworkingConfig': {'EndpointsConfig': networks}}

class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')

def create_collector(spec):
    connection = DockerConnection('localhost', timeout=20)
    try:
        connection.request('POST', '/containers/create?name=' + COLLECTOR,
                           body=json.dumps(spec), headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(65536)
        if response.status != 201:
            raise ValueError('COLLECTOR_CREATE_HTTP_' + str(response.status))
        return json.loads(raw)['Id']
    finally:
        connection.close()

def schema_hash():
    path = Path('/srv/stock-assistant/state/stock-assistant.sqlite3')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        version = db.execute('PRAGMA user_version').fetchone()[0]
    return hashlib.sha256(json.dumps([version, rows], sort_keys=True).encode()).hexdigest()

def public_request(path, body=None, port=9130, timeout=35):
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + path,
        data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(65536))

def wait_health(port):
    for _ in range(30):
        try:
            result = public_request('/health', port=port, timeout=2)
            if result.get('orders_enabled') is not False:
                raise ValueError('ORDERS_DISABLED_CHECK_FAILED')
            if port != 9120 or result.get('status') == 'ok':
                return result
        except (OSError, ValueError):
            time.sleep(.2)
    raise ValueError('SERVICE_HEALTH_TIMEOUT_' + str(port))

def switch_pointer(path, target):
    temp = path.with_name(path.name + '-pending')
    if temp.exists() or temp.is_symlink():
        raise ValueError('UNEXPECTED_TEMP_POINTER')
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, path)

def install(bundle):
    if os.geteuid() != 0:
        raise ValueError('SUDO_REQUIRED')
    sources, override = decode_bundle(bundle)
    old = inspect(COLLECTOR)
    gateway = inspect(GATEWAY)
    if not old['State']['Running'] or not gateway['State']['Running']:
        raise ValueError('EXISTING_SERVICE_NOT_RUNNING')
    if old['Image'] != IMAGE or gateway['Image'] != IMAGE or old['Config']['User'] != '1001:1001':
        raise ValueError('DEPLOYMENT_TARGET_CHANGED')
    if gateway['Config']['Labels'].get('finance.component') != 'isolated-public-market':
        raise ValueError('UNRELATED_GATEWAY_PRESERVED')
    if WRAPPER.is_symlink() or hashlib.sha256(WRAPPER.read_bytes()).hexdigest() != WRAPPER_SHA:
        raise ValueError('COLLECTOR_WRAPPER_CHANGED')
    before = WRAPPER.read_bytes()
    if before.count(OLD_LAUNCHER) != 1:
        raise ValueError('COLLECTOR_HOOK_NOT_UNIQUE')
    processes = command(['docker', 'top', COLLECTOR]).splitlines()[1:]
    if any('refresh-intraday' in line for line in processes):
        raise ValueError('COLLECTOR_ACTIVE_RETRY_WHEN_IDLE')
    local = datetime.now(timezone.utc).timestamp() + 9 * 3600
    hour = datetime.fromtimestamp(local, timezone.utc).hour
    if 9 <= hour < 16:
        raise ValueError('REGULAR_COLLECTION_WINDOW_RETRY_AFTER_16_KST')
    if ROOT.is_symlink() or ROOT.resolve() != ROOT:
        raise ValueError('UNEXPECTED_ROOT')
    if json.loads((ROOT / 'managed.json').read_text()).get('component') != 'isolated-public-market':
        raise ValueError('UNMANAGED_ROOT_PRESERVED')
    if CODE.exists() or CODE.is_symlink() or CACHE.exists() or CACHE.is_symlink():
        raise ValueError('EXISTING_SHARED_PROFILE_PRESERVED')
    for name in ('toss-client-id', 'toss-client-secret'):
        path = Path('/srv/stock-assistant') / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError('EXISTING_CREDENTIAL_FILE_REQUIRED')
    digest = hashlib.sha256(json.dumps({k: bundle['files'][k]['sha256'] for k in sorted(FILES)}, sort_keys=True).encode()).hexdigest()
    release = ROOT / 'releases' / ('shared-' + digest[:20])
    if release.exists() or release.is_symlink():
        raise ValueError('EXISTING_RELEASE_PRESERVED')
    previous_current = (ROOT / 'current').resolve(strict=True)
    if previous_current.parent != ROOT / 'releases':
        raise ValueError('UNEXPECTED_CURRENT_RELEASE')
    labels = old['Config']['Labels']
    # Resolve the override before installing any files. Compose config does not
    # start services or require the staged bind directories to exist.
    base = ['docker', 'compose', '--project-name', labels['com.docker.compose.project'],
            '--env-file', labels['com.docker.compose.project.environment_file'],
            '-f', labels['com.docker.compose.project.config_files'], '-f', '-',
            '--profile', 'candidate', 'config', '--format', 'json']
    resolved_call = subprocess.run(base, input=override.decode(), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    resolved = json.loads(resolved_call.stdout)['services']['stock-assistant']
    if resolved['environment']['STOCK_TOSS_AUTH_CACHE_DIR'] != '/run/stock-shared-auth':
        raise ValueError('OVERRIDE_VALIDATION_FAILED')
    spec = collector_spec(old)
    db_before = schema_hash()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    backup = ROOT / 'backups' / stamp
    backup.mkdir(mode=0o700, parents=True)
    (backup / 'run_intraday_watch.sh').write_bytes(before)
    (backup / 'run_intraday_watch.sh').chmod(0o600)
    release.mkdir(mode=0o750)
    os.chown(release, 0, 1001)
    for name, raw in sources.items():
        target = release / name
        target.write_bytes(raw)
        target.chmod(0o644)
    (release / 'collector.override.yaml').write_bytes(override)
    switch_pointer(CODE, release)
    CACHE.mkdir(mode=0o700)
    os.chown(CACHE, 1001, 1001)
    collector_backup = COLLECTOR + '-backup-' + stamp
    gateway_backup = GATEWAY + '-backup-' + stamp
    new_collector = new_gateway = False
    collector_saved = gateway_saved = False
    try:
        command(['docker', 'stop', '--time', '10', COLLECTOR])
        command(['docker', 'rename', COLLECTOR, collector_backup])
        collector_saved = True
        command(['docker', 'update', '--restart', 'no', collector_backup])
        create_collector(spec)
        new_collector = True
        command(['docker', 'start', COLLECTOR])
        wait_health(9120)
        command(['docker', 'stop', '--time', '10', GATEWAY])
        command(['docker', 'rename', GATEWAY, gateway_backup])
        gateway_saved = True
        command(['docker', 'update', '--restart', 'no', gateway_backup])
        health = "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9130/health',timeout=3).read()"
        args = ['docker', 'create', '--name', GATEWAY, '--restart', 'unless-stopped', '--init', '--read-only',
                '--user', '1001:1001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--pids-limit', '64', '--memory', '192m', '--cpus', '0.5', '--log-driver', 'none', '--network', 'bridge',
                '--publish', '127.0.0.1:9130:9130', '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--label', 'finance.component=isolated-public-market', '--label', 'finance.source=' + digest,
                '--label', 'finance.auth=shared-client-cache',
                '--label', 'com.docker.compose.project=stock-dashboard-public',
                '--label', 'com.docker.compose.service=public-market',
                '--env', 'STOCK_TOSS_AUTH_CACHE_DIR=/run/stock-shared-auth',
                '--mount', 'type=bind,src=' + str(release) + ',dst=/opt/dashboard,readonly',
                '--mount', 'type=bind,src=' + str(CODE) + ',dst=/opt/stock-shared-auth,readonly',
                '--mount', 'type=bind,src=' + str(CACHE) + ',dst=/run/stock-shared-auth',
                '--mount', 'type=bind,src=/srv/stock-assistant/toss-client-id,dst=/run/secrets/toss-client-id,readonly',
                '--mount', 'type=bind,src=/srv/stock-assistant/toss-client-secret,dst=/run/secrets/toss-client-secret,readonly',
                '--health-cmd', 'python -c "' + health + '"', '--health-interval', '30s', '--health-timeout', '5s',
                '--health-retries', '3', '--entrypoint', 'python', IMAGE,
                '/opt/stock-shared-auth/shared_gateway_entry.py']
        command(args)
        new_gateway = True
        command(['docker', 'start', GATEWAY])
        wait_health(9130)
        temp = WRAPPER.with_name(WRAPPER.name + '.shared-auth-pending')
        if temp.exists():
            raise ValueError('UNEXPECTED_WRAPPER_TEMP')
        temp.write_bytes(before.replace(OLD_LAUNCHER, NEW_LAUNCHER))
        stat = WRAPPER.stat()
        temp.chmod(stat.st_mode & 0o777)
        os.chown(temp, stat.st_uid, stat.st_gid)
        os.replace(temp, WRAPPER)
        data = public_request('/public-market', {'operation': 'public_market', 'mode': 'DIRECT', 'symbols': ['240810']})
        if data.get('auth_mode') != 'SHARED_CLIENT_TOKEN_CACHE' or len(data.get('quotes', [])) != 1:
            raise ValueError('PUBLIC_DATA_VERIFICATION_FAILED')
        if db_before != schema_hash():
            raise ValueError('SOURCE_DB_SCHEMA_CHANGED')
        switch_pointer(ROOT / 'current', release)
        manifest = {'checked_at': datetime.now(timezone.utc).isoformat(), 'source_fingerprint': digest,
                    'release': str(release), 'wrapper_sha256': hashlib.sha256(WRAPPER.read_bytes()).hexdigest(),
                    'collector_backup': collector_backup, 'gateway_backup': gateway_backup,
                    'previous_current': str(previous_current), 'rollback_wrapper': str(backup / 'run_intraday_watch.sh'),
                    'collector_id': inspect(COLLECTOR)['Id'], 'gateway_id': inspect(GATEWAY)['Id'],
                    'image_id': IMAGE, 'auth_mode': data['auth_mode'], 'public_data_verified': True,
                    'health': wait_health(9130), 'production_db_schema_unchanged': True,
                    'collector_invoked_for_test': False, 'credential_keys_changed': False,
                    'credential_values_returned': False, 'real_order_sent': False, 'telegram_message_sent': False}
        (release / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        return manifest
    except Exception:
        # Only our newly created containers are discarded; originals are retained.
        WRAPPER.write_bytes(before)
        if new_gateway:
            subprocess.run(['docker', 'rm', '-f', GATEWAY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if gateway_saved:
            command(['docker', 'rename', gateway_backup, GATEWAY])
            command(['docker', 'update', '--restart', gateway['HostConfig']['RestartPolicy']['Name'], GATEWAY])
            command(['docker', 'start', GATEWAY])
        if new_collector:
            subprocess.run(['docker', 'rm', '-f', COLLECTOR], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if collector_saved:
            command(['docker', 'rename', collector_backup, COLLECTOR])
            command(['docker', 'update', '--restart', old['HostConfig']['RestartPolicy']['Name'], COLLECTOR])
            command(['docker', 'start', COLLECTOR])
        raise

if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(500001)
        if len(raw) > 500000:
            raise ValueError('BUNDLE_TOO_LARGE')
        result = install(json.loads(raw))
    except Exception as exc:
        result = {'error': str(exc) if type(exc) is ValueError else type(exc).__name__,
                  'credential_values_returned': False, 'collector_invoked_for_test': False}
    print(json.dumps(result))
    sys.exit(1 if 'error' in result else 0)
