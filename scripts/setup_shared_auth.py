"""Ship approved public source bundle; never transport credential values."""
import argparse
import base64
import hashlib
import json
import subprocess
from bootstrap import ROOT

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    def encode(path):
        raw = path.read_bytes()
        return {'base64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}
    names = ('market_gateway.py', 'market_read_actor.py', 'shared_toss_auth.py',
             'shared_gateway_entry.py', 'shared_collector_entry.py')
    bundle = {'files': {name: encode(ROOT / 'scripts' / name) for name in names},
              'override': encode(ROOT / 'deploy/shared-auth/collector.override.yaml')}
    if not args.install:
        print(json.dumps({'files': {k: v['sha256'] for k, v in bundle['files'].items()},
                          'credentials_in_bundle': False}))
        raise SystemExit(0)
    script = base64.b64encode((ROOT / 'scripts/oracle_install_shared_auth.py').read_bytes()).decode()
    code = 'import base64;exec(base64.b64decode("' + script + '"))'
    call = subprocess.run(['C:/WINDOWS/System32/OpenSSH/ssh.exe', '-T', '-o', 'BatchMode=yes',
                           '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', '144.24.92.159',
                           "sudo -n python3 -c '" + code + "'"], input=json.dumps(bundle), text=True,
                          encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90)
    result = json.loads(call.stdout)
    print(json.dumps(result, indent=2))
    if call.returncode or 'error' in result:
        raise SystemExit(1)
    (ROOT / 'artifacts/oracle-shared-auth-deployment.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
