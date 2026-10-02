"""Run on Oracle over SSH stdin; output presence/metadata only, no secret values."""
import json
import os
import socket
import subprocess
from pathlib import Path

result = {"host":socket.gethostname(), "scope":"read-only metadata", "shell_key_presence":{k:bool(os.environ.get(k)) for k in ("KRX_AUTH_KEY","KRX_API_KEY","DART_API_KEY")},"environment_files":[],"containers":[]}
paths=[Path('/srv/stock-assistant/candidate.env'),Path('/srv/stock-assistant/schedule.env'),Path('/home/ubuntu/.hermes/.env'),Path('/home/ubuntu/.hermes/profiles/stock-cio/.env')]
for path in paths:
    item={"path":str(path),"exists":path.is_file()}
    if item['exists']:
        try:
            variables=[]
            for line in path.read_text().splitlines():
                line=line.strip()
                if '=' in line and not line.startswith('#'):
                    name,value=line.removeprefix('export ').split('=',1)
                    if name.replace('_','').isalnum():
                        variables.append({"name":name,"nonempty":bool(value.strip().strip('\"\''))})
            item['variables_presence_only']=variables
        except PermissionError:
            item['readable']=False
    result['environment_files'].append(item)
try:
    names=subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
    result['running_container_names']=names
    for name in names:
        if 'stock-assistant' not in name:
            continue
        obj=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
        result['containers'].append({"name":name,"image":obj['Config']['Image'],"environment_names_only":[s.split('=',1)[0] for s in obj['Config'].get('Env',[])],"mount_paths_only":[{"source":m['Source'],"destination":m['Destination'],"rw":m['RW']} for m in obj.get('Mounts',[])],"read_only_root":obj['HostConfig']['ReadonlyRootfs']})
except subprocess.CalledProcessError:
    result['docker_metadata']='UNAVAILABLE'
print(json.dumps(result,ensure_ascii=False,indent=2))

