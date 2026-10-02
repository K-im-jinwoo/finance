"""Read-only deployed service checks; never issue provider tokens or orders."""
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

def request(path,body=None,headers=None):
    request=urllib.request.Request('http://127.0.0.1:9130'+path,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type':'application/json',**(headers or {})})
    try:response=urllib.request.urlopen(request,timeout=5)
    except HTTPError as exc:response=exc
    with response:return response.status,json.loads(response.read(4096))

def inspect(name):
    return json.loads(subprocess.check_output(['docker','inspect',name],stderr=subprocess.DEVNULL,text=True))[0]

health_status,health=request('/health')
public_status,public=request('/public-market',{'operation':'public_market','mode':'DIRECT','symbols':['240810']})
order_status,_=request('/orders',{'operation':'order'})
origin_status,_=request('/public-market',{'operation':'public_market','symbols':['240810']},{'Origin':'http://127.0.0.1:8765'})
host_status,_=request('/health',headers={'Host':'external.example:9130'})
service=inspect('stock-dashboard-market-1')
operating=inspect('stock-assistant-stock-assistant-1')
mounts=[{'source':m['Source'],'target':m['Destination'],'rw':m['RW']} for m in service['Mounts']]
assert health_status==200 and health['status']=='AUTH_NOT_REGISTERED'
assert public_status==503 and public['error']=='AUTH_NOT_REGISTERED'
assert order_status==404 and origin_status==403 and host_status==403
assert service['State']['Running'] and operating['State']['Running']
assert service['HostConfig']['ReadonlyRootfs']
assert service['HostConfig']['PortBindings']['9130/tcp']==[{'HostIp':'127.0.0.1','HostPort':'9130'}]
assert all(not mount['rw'] for mount in mounts)
assert all('/var/lib/stock' not in mount['target'] and 'state' not in mount['source'] for mount in mounts)
secret_files=[]
for name in ('dashboard-toss-client-id','dashboard-toss-client-secret'):
    info=(Path('/srv/stock-dashboard/secrets')/name).stat()
    secret_files.append({'name':name,'nonempty':info.st_size>0,'mode':oct(info.st_mode&0o777),'uid':info.st_uid,'gid':info.st_gid})
    assert info.st_size==0 and info.st_mode&0o777==0o600 and info.st_uid==1001
result={'checked_at':datetime.now(timezone.utc).isoformat(),'health':health,
        'missing_auth_http_status':public_status,'order_path_http_status':order_status,
        'browser_origin_http_status':origin_status,'external_host_http_status':host_status,
        'container':service['Name'].lstrip('/'),'state':service['State']['Status'],
        'source_fingerprint':service['Config']['Labels']['finance.source'],
        'read_only_root':True,'host_bind':'127.0.0.1:9130','mounts':mounts,'secret_file_metadata':secret_files,
        'operating_container_state':operating['State']['Status'],'operating_container_started_at':operating['State']['StartedAt'],
        'upstream_token_requested':False,'production_db_changed':False,'credential_values_returned':False,
        'real_order_sent':False,'telegram_message_sent':False,'verification_passed':True}
print(json.dumps(result,indent=2))
