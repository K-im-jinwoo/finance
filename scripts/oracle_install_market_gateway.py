"""Sudo installer for the approved separate public-data service only.

Stdin contains source code and hashes, never credentials. Existing services and
source DBs are untouched. Unknown files/containers/occupied ports fail closed.
"""
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path('/srv/stock-dashboard')
NAME='stock-dashboard-market-1'
IMAGE='stock-investment-assistant:2026-09-28-intraday-dedup-v10'
FILES={'market_gateway.py','market_read_actor.py','register_oracle_market_auth.py'}

def command(args):return subprocess.check_output(args,stderr=subprocess.DEVNULL,text=True)
def install(bundle):
    if os.geteuid()!=0:raise ValueError('SUDO_REQUIRED')
    if set(bundle['files'])!=FILES:raise ValueError('UNEXPECTED_DEPLOYMENT_FILES')
    decoded={}
    for name,item in bundle['files'].items():
        raw=base64.b64decode(item['base64'],validate=True)
        if len(raw)>100000 or hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('SOURCE_HASH_MISMATCH')
        compile(raw,name,'exec')
        decoded[name]=raw
    operating=json.loads(command(['docker','inspect','stock-assistant-stock-assistant-1']))[0]
    if operating['Config']['Image']!=IMAGE:raise ValueError('OPERATING_IMAGE_CHANGED_RECHECK_REQUIRED')
    image_id=json.loads(command(['docker','image','inspect',IMAGE]))[0]['Id']
    if image_id!=operating['Image']:raise ValueError('IMAGE_TAG_MOVED_RECHECK_REQUIRED')
    names=command(['docker','ps','-a','--format','{{.Names}}']).splitlines()
    existing=None
    if NAME in names:
        existing=json.loads(command(['docker','inspect',NAME]))[0]
        if existing['Config'].get('Labels',{}).get('finance.component')!='isolated-public-market':raise ValueError('UNRELATED_CONTAINER_PRESERVED')
    else:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',9130))
    if ROOT.is_symlink() or ROOT.resolve()!=ROOT:raise ValueError('UNEXPECTED_DEPLOYMENT_TARGET')
    marker=ROOT/'managed.json'
    if ROOT.exists() and any(ROOT.iterdir()) and not marker.is_file():raise ValueError('EXISTING_DIRECTORY_PRESERVED')
    ROOT.mkdir(mode=0o750,exist_ok=True)
    os.chown(ROOT,0,1001)
    if marker.exists() and json.loads(marker.read_text()).get('component')!='isolated-public-market':raise ValueError('UNRELATED_DIRECTORY_PRESERVED')
    if not marker.exists():marker.write_text(json.dumps({'component':'isolated-public-market'}))
    for name in ('secrets','releases'):
        folder=ROOT/name
        if folder.is_symlink():raise ValueError('UNEXPECTED_DEPLOYMENT_TARGET')
        folder.mkdir(mode=0o750,exist_ok=True)
        os.chown(folder,0,1001)
    for name in ('dashboard-toss-client-id','dashboard-toss-client-secret','toss-client-id'):
        target=ROOT/'secrets'/name
        if target.is_symlink():raise ValueError('UNEXPECTED_SECRET_TARGET')
        if not target.exists():
            target.touch(mode=0o600)
            os.chown(target,1001,1001)
    fingerprint=hashlib.sha256(json.dumps({name:bundle['files'][name]['sha256'] for name in sorted(FILES)},sort_keys=True).encode()).hexdigest()
    release=ROOT/'releases'/fingerprint[:20]
    if release.is_symlink():raise ValueError('UNEXPECTED_DEPLOYMENT_TARGET')
    release.mkdir(mode=0o755,exist_ok=True)
    for name,raw in decoded.items():
        path=release/name
        if path.exists() and path.read_bytes()!=raw:raise ValueError('EXISTING_RELEASE_CHANGED')
        if not path.exists():path.write_bytes(raw);path.chmod(0o644)
    manifest={'checked_at':datetime.now(timezone.utc).isoformat(),'component':'isolated-public-market',
              'source_fingerprint':fingerprint,'files':{k:bundle['files'][k]['sha256'] for k in sorted(FILES)},
              'image_id':image_id,'container':NAME,'host_bind':'127.0.0.1:9130','orders_enabled':False,'production_db_changed':False}
    (release/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    current=ROOT/'current'
    if current.exists() and not current.is_symlink():raise ValueError('UNEXPECTED_CURRENT_POINTER')
    next_link=ROOT/'next-current'
    if next_link.exists() or next_link.is_symlink():raise ValueError('UNEXPECTED_TEMP_POINTER')
    next_link.symlink_to(release,target_is_directory=True)
    os.replace(next_link,current)
    if existing and existing['Config'].get('Labels',{}).get('finance.source')==fingerprint:
        if existing['State']['Status']!='running':command(['docker','start',NAME])
        manifest['deployment_action']='EXISTING_MANAGED_SERVICE_REUSED'
        return manifest
    if existing:
        # Retain the prior service as a stopped rollback container; never delete it.
        backup_name=NAME+'-backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        command(['docker','stop','--time','10',NAME])
        command(['docker','rename',NAME,backup_name])
        manifest['rollback_container']=backup_name
    health="import urllib.request;urllib.request.urlopen('http://127.0.0.1:9130/health',timeout=3).read()"
    args=['docker','run','-d','--name',NAME,'--restart','unless-stopped','--init','--read-only','--user','1001:1001',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','64','--memory','192m','--cpus','0.5',
          '--log-driver','none','--network','bridge','--publish','127.0.0.1:9130:9130',
          '--tmpfs','/tmp:rw,noexec,nosuid,size=16m','--label','finance.component=isolated-public-market','--label','finance.source='+fingerprint,
          '--mount','type=bind,src='+str(release)+',dst=/opt/dashboard,readonly',
          '--mount','type=bind,src='+str(ROOT/'secrets')+',dst=/run/secrets,readonly',
          '--mount','type=bind,src=/srv/stock-assistant/toss-client-id,dst=/run/secrets/toss-client-id,readonly',
          '--health-cmd','python -c "'+health+'"','--health-interval','30s','--health-timeout','5s','--health-retries','3',
          '--entrypoint','python',image_id,'/opt/dashboard/market_gateway.py','--bind','0.0.0.0','--port','9130']
    command(args)
    after=json.loads(command(['docker','inspect','stock-assistant-stock-assistant-1']))[0]
    if after['Id']!=operating['Id'] or after['State']['StartedAt']!=operating['State']['StartedAt']:
        raise ValueError('OPERATING_CONTAINER_CHANGED_DURING_SETUP')
    manifest.update(deployment_action='SEPARATE_SERVICE_CREATED',operating_container_unchanged=True)
    return manifest

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(500001)
        if len(raw)>500000:raise ValueError('BUNDLE_TOO_LARGE')
        result=install(json.loads(raw))
    except Exception as exc:
        # Deployment bundle never includes credentials. Still suppress command stderr.
        result={'error':str(exc) if type(exc) is ValueError else type(exc).__name__,'production_db_changed':False}
    print(json.dumps(result))
    sys.exit(1 if 'error' in result else 0)
