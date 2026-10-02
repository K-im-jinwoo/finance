"""Only names, presence and deployment metadata. Never return credential values."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

result={'checked_at':datetime.now(timezone.utc).isoformat(),'secret_files':[],'containers':[],
        'candidate_variable_names':[],'credential_values_returned':False}
env=Path('/srv/stock-assistant/candidate.env')
if env.is_file():
    for line in env.read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            name,value=line.split('=',1)
            if any(word in name.upper() for word in ('TOSS','DASHBOARD')):
                result['candidate_variable_names'].append({'name':name.strip(),'nonempty':bool(value.strip())})
for directory in ('/srv/stock-assistant/secrets','/srv/hermes-ops/secrets','/srv/stock-dashboard/secrets'):
    root=Path(directory)
    try:
        for path in root.iterdir() if root.is_dir() else ():
            if path.is_file() and any(word in path.name.lower() for word in ('toss','dashboard')):
                st=path.stat()
                result['secret_files'].append({'path':str(path),'nonempty':st.st_size>0,'mode':oct(st.st_mode & 0o777),'owner_uid':st.st_uid})
    except PermissionError:
        result['secret_files'].append({'directory':directory,'readable':False})
names=subprocess.check_output(['docker','ps','--format','{{.Names}}'],text=True).splitlines()
for name in names:
    if 'stock' not in name:
        continue
    info=json.loads(subprocess.check_output(['docker','inspect',name],text=True))[0]
    result['containers'].append({'name':name,'image':info['Config']['Image'],'user':info['Config']['User'],
                                'network_mode':info['HostConfig']['NetworkMode'],'mounts':[{'source':m['Source'],'target':m['Destination'],'rw':m['RW']} for m in info.get('Mounts',[])],
                                'environment_names':[v.split('=',1)[0] for v in info['Config'].get('Env',[])],
                                'state':info['State']['Status']})
result['sudo_available']=subprocess.run(['sudo','-n','true'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
print(json.dumps(result,ensure_ascii=False,indent=2))
