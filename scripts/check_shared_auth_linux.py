"""Synthetic POSIX checks in the existing new service's temporary filesystem only."""
import base64
import json
import subprocess
from bootstrap import ROOT

CHECK='''import base64,json,os,pathlib,subprocess,sys,tempfile
bundle=json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix="shared-auth-check-") as folder:
    root=pathlib.Path(folder)
    for name,encoded in bundle.items():
        assert name in {"shared_toss_auth.py","test_shared_toss_auth.py"}
        (root/name).write_bytes(base64.b64decode(encoded))
    runner="""import io,json,unittest,sys
if __name__=='__main__':
    output=io.StringIO()
    suite=unittest.defaultTestLoader.loadTestsFromName('test_shared_toss_auth')
    result=unittest.TextTestRunner(stream=output).run(suite)
    print(json.dumps({'tests_run':result.testsRun,'errors':len(result.errors),'failures':len(result.failures),'skipped':len(result.skipped),'passed':result.wasSuccessful(),'real_token_issued':False,'production_db_changed':False}))
    sys.exit(0 if result.wasSuccessful() else 1)
"""
    (root/'runner.py').write_text(runner)
    env={**os.environ,'PYTHONPATH':str(root)+os.pathsep+os.environ.get('PYTHONPATH','')}
    call=subprocess.run([sys.executable,str(root/'runner.py')],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,timeout=40)
    print(call.stdout,end='')
    sys.exit(call.returncode)
'''

if __name__=='__main__':
    payload=json.dumps({name:base64.b64encode(path.read_bytes()).decode() for name,path in {
        'shared_toss_auth.py':ROOT/'scripts/shared_toss_auth.py',
        'test_shared_toss_auth.py':ROOT/'tests/test_shared_toss_auth.py'}.items()})
    code='import base64;exec(base64.b64decode("'+base64.b64encode(CHECK.encode()).decode()+'"))'
    command="docker exec -i stock-dashboard-market-1 python -c '"+code+"'"
    call=subprocess.run(['C:/WINDOWS/System32/OpenSSH/ssh.exe','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10','144.24.92.159',command],input=payload,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=60)
    result=json.loads(call.stdout)
    (ROOT/'artifacts/shared-auth-linux-verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    raise SystemExit(call.returncode)
