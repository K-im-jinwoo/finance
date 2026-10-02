"""Read stock Hermes storage metadata only; do not invoke agents or expose secrets."""
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

result={'scope':'stock Hermes storage metadata only','profiles':[],'gateway_mounts':[]}
base=Path('/home/ubuntu/.hermes/profiles')
for name in ('stock-cio','stock-market','stock-fundamentals','stock-risk','stock-live-market'):
    root=base/name
    item={'profile':name,'exists':root.is_dir(),'entries':[],'databases':[]}
    if root.is_dir():
        item['entries']=[{'name':p.name,'directory':p.is_dir()} for p in sorted(root.iterdir()) if p.name not in ('.env','auth.json','credentials.json')]
        for folder in (root,root/'sessions',root/'state',root/'cron'):
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if path.is_file() and path.suffix in ('.db','.sqlite','.sqlite3'):
                    entry={'path':str(path.relative_to(root)),'tables':{}}
                    try:
                        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as connection:
                            connection.execute('PRAGMA query_only=ON')
                            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                                if table.replace('_','').isalnum():
                                    entry['tables'][table]={'columns':[r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')],
                                                            'rows':connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]}
                    except Exception as error:
                        entry['error_type']=type(error).__name__
                    item['databases'].append(entry)
        for folder in (root/'cron',root/'cron/output',root/'reports',root/'kanban',root/'sessions'):
            if folder.is_dir():
                item.setdefault('output_directories',[]).append({'path':str(folder.relative_to(root)),
                    'file_count':sum(p.is_file() for p in folder.iterdir()),'sample_filenames':[p.name for p in sorted(folder.iterdir())[:12]]})
    result['profiles'].append(item)
try:
    obj=json.loads(subprocess.check_output(['docker','inspect','deploy-hermes-gateway-1'],text=True))[0]
    result['gateway_mounts']=[{'source':m['Source'],'destination':m['Destination'],'rw':m['RW']} for m in obj['Mounts']]
except Exception as error:
    result['gateway_error_type']=type(error).__name__
print(json.dumps(result,ensure_ascii=False,indent=2))
