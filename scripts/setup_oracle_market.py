"""Install verified local gateway sources only. No credential inputs or output."""
import argparse
import base64
import hashlib
import json
import subprocess
from bootstrap import ROOT

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--install',action='store_true')
    args=parser.parse_args()
    files={}
    for name in ('market_gateway.py','market_read_actor.py','register_oracle_market_auth.py'):
        raw=(ROOT/'scripts'/name).read_bytes()
        files[name]={'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode()}
    payload=json.dumps({'files':files})
    manifest={'files':{name:item['sha256'] for name,item in files.items()},'credentials_in_bundle':False}
    if args.install:
        script=base64.b64encode((ROOT/'scripts/oracle_install_market_gateway.py').read_bytes()).decode()
        code='import base64;exec(base64.b64decode("'+script+'"))'
        command="sudo -n python3 -c '"+code+"'"
        call=subprocess.run(['C:/WINDOWS/System32/OpenSSH/ssh.exe','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
                             '144.24.92.159',command],input=payload,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=60)
        result=json.loads(call.stdout)
        if call.returncode or 'error' in result:
            print(json.dumps(result));raise SystemExit(1)
        manifest['deployment']=result
        (ROOT/'artifacts/oracle-market-gateway-deployment.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest,indent=2))
