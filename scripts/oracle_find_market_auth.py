"""Bounded auth-name inventory and identity comparisons; no values or hashes."""
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

operating=Path('/srv/stock-assistant/toss-client-id').read_text().strip()
files=set()
for folder in ('/srv/stock-assistant','/srv/hermes-ops','/srv/stock-dashboard','/home/ubuntu/.hermes'):
    root=Path(folder)
    for base,dirs,names in os.walk(root):
        depth=len(Path(base).relative_to(root).parts)
        dirs[:]=[d for d in dirs if depth<3 and d not in {'state','logs','sessions','output','backups','releases','node_modules','.git','memory','history'}]
        for name in names:
            if name=='.env' or name.endswith('.env') or name.startswith('env.') or ('toss' in name.lower() and name.endswith(('.json','.env','.yaml','.yml'))):
                p=Path(base)/name
                if not p.is_symlink() and p.stat().st_size<=200000:files.add(p)
result={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'bounded Oracle environment configuration','credential_values_returned':False,'files':[]}
for path in sorted(files):
    variables=[]
    for line in path.read_text(errors='replace').splitlines():
        match=re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$',line)
        if match and any(key in match[1].upper() for key in ('TOSS','DASHBOARD','MARKET_DATA_CLIENT')):
            value=match[2].strip().strip('\"\'')
            item={'name':match[1],'nonempty':bool(value)}
            if 'CLIENT_ID' in match[1] and 'FILE' not in match[1]:
                item['different_from_operating']=bool(value) and value!=operating
            if match[1].endswith(('_HOST_FILE','_FILE')) and value.startswith('/'):
                referenced=Path(value)
                if referenced.is_file():
                    actual=referenced.read_text().strip()
                    item.update(referenced_file_exists=True,referenced_file_nonempty=bool(actual))
                    if 'CLIENT_ID' in match[1]:item['different_from_operating']=bool(actual) and actual!=operating
            variables.append(item)
    if variables:result['files'].append({'path':str(path),'variables':variables})
result['configuration_files_checked']=len(files)
print(json.dumps(result,ensure_ascii=False,indent=2))
