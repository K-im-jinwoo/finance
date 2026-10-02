"""Actual public-data/auth reuse checks. No collector, DB writes, or messages."""
import hashlib
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path('/srv/stock-dashboard')
cache = ROOT / 'shared-auth-cache'

def run(args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)

def request(path, body=None, headers=None):
    req = urllib.request.Request('http://127.0.0.1:9130' + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read(65536))

code = '''import json,sys
from pathlib import Path
sys.path.insert(0,'/opt/stock-shared-auth')
from shared_toss_auth import SharedTossMarketDataClient
cache=Path('/run/stock-shared-auth/toss-token.json')
before=cache.stat().st_mtime_ns
client=SharedTossMarketDataClient(Path('/run/secrets/toss-client-id').read_text().strip(),Path('/run/secrets/toss-client-secret').read_text().strip())
token=client._token()
stored=json.loads(cache.read_text())['token']
from datetime import datetime,timezone
quotes=client.prices(['240810'],observed_at=datetime.now(timezone.utc))
assert token==stored and cache.stat().st_mtime_ns==before
assert len(quotes)==1 and quotes[0].symbol=='240810'
print(json.dumps({'token_reused':True,'cache_unchanged':True,'quote_read':True,'module_path':str(Path(__import__('shared_toss_auth').__file__)),'token_returned':False}))
'''
status, data = request('/public-market', {'operation': 'public_market', 'mode': 'DIRECT',
                                         'symbols': ['240810', '054920', '044450', '053800', '062040']})
assert status == 200 and data['auth_mode'] == 'SHARED_CLIENT_TOKEN_CACHE'
assert len(data['quotes']) == 5 and data['calendar']['source'] == 'TOSS_SECURITIES_OPEN_API'
before = (cache / 'toss-token.json').stat().st_mtime_ns
collector = json.loads(run(['docker', 'exec', 'stock-assistant-stock-assistant-1', 'python', '-c', code]))
compose = '/srv/stock-assistant/releases/f3af37a-wip-20260928-intraday-dedup-v10/deploy/compose.yaml'
selected = json.loads(run(['docker', 'compose', '--env-file', '/srv/stock-assistant/candidate.env',
                           '-f', compose, '--profile', 'candidate', 'exec', '-T', 'stock-assistant', 'python', '-c', code]))
assert selected == collector
manifest = json.loads((ROOT / 'current/manifest.json').read_text())
old = json.loads(run(['docker', 'inspect', manifest['collector_backup']]))[0]
current = json.loads(run(['docker', 'inspect', 'stock-assistant-stock-assistant-1']))[0]
assert not old['State']['Running'] and old['HostConfig']['RestartPolicy']['Name'] == 'no'
assert current['Config']['Env'] == old['Config']['Env'] + ['STOCK_TOSS_AUTH_CACHE_DIR=/run/stock-shared-auth']
assert {k: v for k, v in current['Config'].items() if k != 'Env'} == {k: v for k, v in old['Config'].items() if k != 'Env'}
assert {k: v for k, v in current['HostConfig'].items() if k not in {'Mounts', 'RestartPolicy'}} == {k: v for k, v in old['HostConfig'].items() if k not in {'Mounts', 'RestartPolicy'}}
assert current['HostConfig']['RestartPolicy']['Name'] == 'unless-stopped'
assert current['HostConfig']['Mounts'][:len(old['HostConfig']['Mounts'])] == old['HostConfig']['Mounts']
old_mounts = {m['Destination']: (m['Source'], m['RW']) for m in old['Mounts']}
new_mounts = {m['Destination']: (m['Source'], m['RW']) for m in current['Mounts']}
assert all(new_mounts[path] == value for path, value in old_mounts.items())
cli_help = run(['docker', 'exec', 'stock-assistant-stock-assistant-1', 'python',
                '/opt/stock-shared-auth/shared_collector_entry.py', 'refresh-intraday', '--help'])
assert '--client-id-file' in cli_help and '--client-secret-file' in cli_help
source_names = ('market_gateway.py', 'market_read_actor.py', 'shared_toss_auth.py', 'shared_gateway_entry.py', 'shared_collector_entry.py')
actual_digest = hashlib.sha256(json.dumps({name: hashlib.sha256((ROOT / 'current' / name).read_bytes()).hexdigest()
                                          for name in sorted(source_names)}, sort_keys=True).encode()).hexdigest()
assert actual_digest == manifest['source_fingerprint']
status, repeated = request('/public-market', {'operation': 'public_market', 'mode': 'DIRECT', 'symbols': ['240810']})
assert status == 200 and (cache / 'toss-token.json').stat().st_mtime_ns == before
assert request('/orders', {'operation': 'order'})[0] == 404
assert request('/public-market', {'operation': 'public_market', 'symbols': ['240810']},
               {'Origin': 'http://127.0.0.1:8765'})[0] == 403
assert request('/health', headers={'Host': 'external.example:9130'})[0] == 403
_, health = request('/health')
assert health['status'] == 'AUTH_VERIFIED' and health['orders_enabled'] is False
services = {}
for name in ('stock-assistant-stock-assistant-1', 'stock-dashboard-market-1'):
    item = json.loads(run(['docker', 'inspect', name]))[0]
    assert item['State']['Running'] and item['RestartCount'] == 0
    mounts = {m['Destination']: m for m in item['Mounts']}
    assert mounts['/run/stock-shared-auth']['Source'] == str(cache) and mounts['/run/stock-shared-auth']['RW']
    assert not mounts['/opt/stock-shared-auth']['RW']
    if name == 'stock-dashboard-market-1':
        assert '/var/lib/stock' not in mounts
        assert item['HostConfig']['PortBindings']['9130/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': '9130'}]
        assert item['Config']['Labels']['com.docker.compose.project'] == 'stock-dashboard-public'
    services[name] = {'started_at': item['State']['StartedAt'], 'restart_count': item['RestartCount'],
                      'image_id': item['Image'], 'health': item['State'].get('Health', {}).get('Status')}
for path in (cache, cache / 'toss-token.json', cache / 'toss-token.lock'):
    info = path.stat()
    assert info.st_uid == 1001 and info.st_gid == 1001
    assert info.st_mode & 0o777 == (0o700 if path == cache else 0o600)
wrapper = Path(compose).parent / 'jobs/run_intraday_watch.sh'
outer = Path('/home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-intraday-alerts.sh')
assert hashlib.sha256(outer.read_bytes()).hexdigest() == '413d6d0af5fd46aa249eb51ef8dace0213f5c79fbfd60a81c11cf27fedb04341'
assert wrapper.read_bytes().count(b'python /opt/stock-shared-auth/shared_collector_entry.py refresh-intraday') == 1
result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'health': health, 'services': services,
          'collector_auth': collector, 'compose_exec_selects_current_collector': selected == collector,
          'shared_token_cache_unchanged_after_repeated_reads': True,
          'quotes': data['quotes'], 'calendar': data['calendar'],
          'cache_directory_mode': '0700', 'cache_files_mode': '0600',
          'outer_wrapper_unchanged': True, 'orders_path_http_status': 404,
          'collector_config_preserved_except_auth_additions': True,
          'collector_existing_mounts_preserved': True, 'collector_cli_help_verified_without_execution': True,
          'deployed_source_fingerprint_verified': True,
          'browser_origin_http_status': 403, 'external_host_http_status': 403,
          'credential_keys_changed': False, 'credential_values_returned': False,
          'production_db_changed_by_verification': False, 'collector_invoked_for_test': False,
          'telegram_message_sent': False, 'verification_passed': True}
print(json.dumps(result, indent=2))
