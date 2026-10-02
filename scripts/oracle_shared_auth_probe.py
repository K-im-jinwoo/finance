"""Read-only hook inventory. Never return auth values or execute a collector."""
import hashlib
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone

info=json.loads(subprocess.check_output(['docker','inspect','stock-assistant-stock-assistant-1'],text=True))[0]
labels=info['Config'].get('Labels',{})
result={'checked_at':datetime.now(timezone.utc).isoformat(),'container':info['Name'].lstrip('/'),
        'started_at':info['State']['StartedAt'],'image':info['Config']['Image'],
        'compose_files':labels.get('com.docker.compose.project.config_files'),
        'compose_working_dir':labels.get('com.docker.compose.project.working_dir'),
        'credential_values_returned':False,'production_db_changed':False,'wrappers':[]}
compose_path=Path(labels['com.docker.compose.project.config_files'])
for name in ('/home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-intraday-alerts.sh',
             str(compose_path.parent/'jobs/run_intraday_watch.sh')):
    path=Path(name)
    item={'path':name,'exists':path.is_file()}
    if path.is_file():
        raw=path.read_bytes();source=raw.decode()
        item.update(source_sha256=hashlib.sha256(raw).hexdigest(),
                    collector_launcher_matches=source.count('python -m stock_assistant refresh-intraday'),
                    uses_client_files='--client-id-file' in source and '--client-secret-file' in source)
    result['wrappers'].append(item)
print(json.dumps(result,indent=2))
