"""Bounded deployment metadata only; no credentials or collector execution."""
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

def run(args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)

items = {}
for name in ('stock-assistant-stock-assistant-1', 'stock-dashboard-market-1'):
    data = json.loads(run(['docker', 'inspect', name]))[0]
    labels = data['Config'].get('Labels', {})
    top_result = subprocess.run(['docker', 'top', name], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    top = top_result.stdout.splitlines()[1:]
    items[name] = {
        'id': data['Id'], 'image_id': data['Image'], 'image': data['Config']['Image'],
        'started_at': data['State']['StartedAt'], 'running': data['State']['Running'],
        'user': data['Config']['User'], 'restart': data['HostConfig']['RestartPolicy'],
        'environment_names': sorted(value.split('=', 1)[0] for value in data['Config']['Env']),
        'labels': {key: value for key, value in labels.items()
                   if key.startswith('com.docker.compose.') or key.startswith('finance.')},
        'mounts': [{'source': m['Source'], 'target': m['Destination'], 'rw': m['RW']}
                   for m in data['Mounts']],
        'ports': data['HostConfig']['PortBindings'],
        'collector_process_count': sum('refresh-intraday' in line for line in top),
        'process_inventory_available': top_result.returncode == 0,
    }
outer = Path('/home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-intraday-alerts.sh')
source = outer.read_text()
result = {'checked_at': datetime.now(timezone.utc).isoformat(), 'containers': items,
          'outer_wrapper_sha256': hashlib.sha256(outer.read_bytes()).hexdigest(),
          'outer_env_paths': sorted(set(re.findall(r'/srv/stock-assistant/[A-Za-z0-9_.-]+\.env', source))),
          'credential_values_returned': False, 'production_db_changed': False}
print(json.dumps(result, indent=2))
