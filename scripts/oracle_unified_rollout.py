"""Deployment driver for the user's explicitly approved Oracle/Telegram cutover."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import sys

ROOT=Path(__file__).resolve().parents[1]
SSH='C:/WINDOWS/System32/OpenSSH/ssh.exe'
SCP='C:/WINDOWS/System32/OpenSSH/scp.exe'
HOST='144.24.92.159'
OPTIONS=['-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10']


def call(args,timeout,input_text=None):
    p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=timeout,
                     input=input_text,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode:raise RuntimeError(p.stderr[-3000:] or 'REMOTE_OPERATION_FAILED')
    return p.stdout


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('stage','validate','deploy','rollback','repair-health'))
    parser.add_argument('--stamp')
    args=parser.parse_args()
    stamp=args.stamp or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if not stamp.isalnum():raise ValueError('INVALID_RELEASE_ID')
    input_text=None
    if args.mode=='repair-health':
        command=shlex.join(['sudo','-n','python3','-',args.mode,stamp])
        input_text=(ROOT/'scripts/oracle_unified_rollout_host.py').read_text(encoding='utf-8')
    elif args.mode=='stage':
        package=Path(tempfile.gettempdir())/(stamp+'-unified.tar.gz')
        if package.exists():raise ValueError('LOCAL_BUNDLE_ALREADY_EXISTS')
        with tarfile.open(package,'w:gz') as tar:
            for p in sorted((ROOT/'engine/src').rglob('*.py')):
                if '__pycache__' not in p.parts:tar.add(p,arcname=str(p.relative_to(ROOT/'engine')).replace('\\','/'))
            for relative in (
                'deploy/Dockerfile', 'deploy/jobs/run_stock_cycle.sh',
                'deploy/jobs/run_stock_morning.py', 'deploy/jobs/stock-morning.sh',
                'hermes/profiles/stock-cio/SOUL.md', 'hermes/skills/stock-research/SKILL.md',
            ):
                tar.add(ROOT/'engine'/relative,arcname=relative)
            tar.add(ROOT/'scripts/oracle_unified_rollout_host.py',arcname='deploy/unified_rollout_host.py')
        remote='/tmp/'+package.name
        call([SCP,*OPTIONS,str(package),HOST+':'+remote],120)
        code="import tarfile,sys;f=tarfile.open(sys.argv[3]);exec(compile(f.extractfile('deploy/unified_rollout_host.py').read(),'unified_rollout_host.py','exec'))"
        command=shlex.join(['sudo','-n','python3','-c',code,'stage',stamp,remote])
    else:
        command=shlex.join(['sudo','-n','python3','/srv/stock-assistant/releases/'+stamp+'-unified/deploy/unified_rollout_host.py',args.mode,stamp])
    print('release_id='+stamp,flush=True)
    print(call([SSH,'-T',*OPTIONS,HOST,command],2100,input_text=input_text),flush=True)

if __name__=='__main__':main()
