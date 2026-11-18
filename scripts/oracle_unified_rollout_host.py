"""Run on the approved Oracle host. Preserve runtime, backup, validate, then switch."""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT=Path('/srv/stock-assistant')
CONTAINER='stock-assistant-stock-assistant-1'
PROFILE=Path('/home/ubuntu/.hermes/profiles/stock-cio')
def run(args,timeout=900):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode: raise RuntimeError('COMMAND_FAILED:'+args[0]+':'+p.stderr[-1200:])
    return p.stdout
def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def atomic(path,data,mode=None):
    info=path.stat() if path.exists() else None
    temp=path.with_name(path.name+'.unified-pending')
    if temp.exists():raise ValueError('TEMP_FILE_ALREADY_EXISTS')
    temp.write_bytes(data)
    os.chmod(temp,mode or (info.st_mode & 0o777 if info else 0o600))
    if info:os.chown(temp,info.st_uid,info.st_gid)
    os.replace(temp,path)
def snapshot(source,target,uid=None,gid=None):
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db, sqlite3.connect(target) as backup:db.backup(backup)
    os.chmod(target,0o600)
    if uid is not None:os.chown(target,uid,gid)
def paths(stamp):
    if not stamp.isalnum():raise ValueError('INVALID_RELEASE_ID')
    return ROOT/'releases'/(stamp+'-unified'),ROOT/'backups'/(stamp+'-unified')
def stage(stamp,bundle):
    release,backup=paths(stamp)
    if release.exists() or backup.exists():raise ValueError('RELEASE_ALREADY_EXISTS')
    old=inspect(CONTAINER)
    if not old['State']['Running'] or old['Config']['User']!='1001:1001':raise ValueError('DEPLOYMENT_TARGET_CHANGED')
    backup.mkdir(mode=0o700)
    (backup/'container.json').write_text(json.dumps(old));os.chmod(backup/'container.json',0o600)
    (backup/'previous-current').write_text(str((ROOT/'current').resolve()))
    files=[ROOT/'schedule.env',ROOT/'candidate.env',PROFILE/'SOUL.md',PROFILE/'cron/jobs.json',PROFILE/'skills/stock-research/SKILL.md']
    manifest=[]
    for number,path in enumerate(files):
        if path.is_file():
            target=backup/('file-'+str(number));shutil.copy2(path,target);os.chmod(target,0o600)
            info=path.stat();manifest.append({'path':str(path),'backup':str(target),'uid':info.st_uid,'gid':info.st_gid,
                                            'mode':info.st_mode&0o777,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (backup/'files.json').write_text(json.dumps(manifest))
    snapshot(ROOT/'state/stock-assistant.sqlite3',backup/'source.sqlite3',1001,1001)
    study=ROOT/'state/unified-research-inputs.sqlite3'
    if study.exists():snapshot(study,backup/'previous-research.sqlite3')
    shutil.copytree((ROOT/'current').resolve(),release,symlinks=True)
    with tarfile.open(bundle) as archive:
        for member in archive.getmembers():
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or not member.isfile():raise ValueError('UNSAFE_BUNDLE_MEMBER')
            target=release/p
            if not target.resolve().is_relative_to(release.resolve()):raise ValueError('BUNDLE_ESCAPES_RELEASE')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    image='stock-investment-assistant:'+stamp.lower()+'-unified'
    run(['docker','build','-t',image,'-f',str(release/'deploy/Dockerfile'),str(release)],timeout=1200)
    (backup/'new-image').write_text(image)
    print(json.dumps({'status':'STAGED','release':str(release),'backup':str(backup),'image':image,'source_snapshot_bytes':(backup/'source.sqlite3').stat().st_size}))
def validate(stamp):
    release,backup=paths(stamp);old=json.loads((backup/'container.json').read_text())
    image=(backup/'new-image').read_text()
    cmd=['docker','run','--rm','--user',old['Config']['User'],'--read-only','--entrypoint','python','--tmpfs','/tmp:size=64m']
    for entry in old['Config']['Env']:cmd+=['-e',entry]
    cmd+=['-e','STOCK_UNIFIED_ENABLED=true','-e','STOCK_UNIFIED_RESEARCH_DATABASE=/var/lib/stock/unified-research-inputs.sqlite3']
    for mount in old['Mounts']:
        cmd+=['--mount','type=bind,src='+mount['Source']+',dst='+mount['Destination']+('' if mount['RW'] else ',readonly')]
    cmd+=['--mount','type=bind,src='+str(backup/'source.sqlite3')+',dst=/var/lib/source.sqlite3,readonly',image,'-c']
    code="""from pathlib import Path
from datetime import datetime,timezone
import json
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.research_data import ResearchData
from stock_assistant.unified_research import financial_universe
from stock_assistant.unified_cycle import refresh_unified,shared_calendar
from stock_assistant.providers.dart import DartClient
from stock_assistant.providers.news import NaverNewsClient
from stock_assistant.news_cost import NewsApiBudget
repo=ReadOnlyResearchRepository(Path('/var/lib/source.sqlite3'));now=datetime.now(timezone.utc)
store=ResearchData(Path('/var/lib/stock/unified-research-inputs.sqlite3'),source_database=repo.database_path)
eligible=[]
for s,f,_ in financial_universe(repo,store,as_of=now):
 if f.gate=='PASS':eligible.append((-(f.financial_score or 0),s.symbol))
symbols=[s for _,s in sorted(eligible)[:8]]
key=Path('/run/secrets/dart-api-key').read_text().strip()
news=NaverNewsClient(Path('/run/secrets/naver-news-client-id').read_text().strip(),Path('/run/secrets/naver-news-client-secret').read_text().strip(),budget=NewsApiBudget(Path('/var/lib/stock/news-api-usage.sqlite3')))
calendar=shared_calendar(Path('/run/secrets/toss-client-id').read_text().strip(),Path('/run/secrets/toss-client-secret').read_text().strip())
report=refresh_unified(repo,store,dart_client=DartClient(key),news_client=news,calendar_client=calendar,dart_key=key,seed_symbols=symbols or None,batch_size=8,archive_pages=5)
visible={k:report[k] for k in ['report_id','as_of','contract','policy_version','thresholds','research_windows','coverage_scope','orders_enabled','paper_account_eligible','research_progress','decision_counts','pending_reason_counts','refresh_stages']}
visible['candidates']=[{k:r.get(k) for k in ['symbol','name','action_decision','financial_score','news_score','risk_penalty','total_score','recent_news_complete','risk_history_complete']} for r in report['candidates']]
visible['collected_bundle_count']=sum(r['payload'].get('status')=='COLLECTED' for r in store.latest_by_symbol('financial_bundle',as_of=datetime.now(timezone.utc)).values())
visible['archive_inputs_count']=sum(len(store.records(kind,s,as_of=datetime.now(timezone.utc))) for s in symbols for kind in ['recent_archive_article','risk_archive_article'])
from collections import Counter
visible['financial_reviews']=[{**{k:r.get(k) for k in ['symbol','name','financial_score','news_score','total_score','action_decision','recent_news_complete','risk_history_complete']},
                              'pending_counts':dict(Counter(reason.split(':',1)[0] for reason in r['pending_checks']))}
                             for r in report['evaluations'] if r.get('symbol') in symbols]
print(json.dumps(visible,ensure_ascii=False))
"""
    cmd.append(code)
    output=run(cmd,timeout=1800)
    result=json.loads(output)
    if result['contract']!='UNIFIED_RESEARCH_V1':raise ValueError('VALIDATION_CONTRACT_FAILED')
    if (result.get('policy_version')!='unified-research-2026-10-14.1'
            or result.get('thresholds')!={'financial':70,'total':75}
            or result.get('orders_enabled') is not False or result.get('paper_account_eligible') is not False):
        raise ValueError('APPROVED_SPLIT_POLICY_CONTRACT_FAILED')
    if result.get('refresh_stages',{}).get('news_budget',{}).get('actual_calls',0)>5:
        raise ValueError('NEWS_BUDGET_EXPANDED')
    if any(r['action_decision']=='BUY_RECOMMENDATION' and not (r.get('recent_news_complete') and r.get('risk_history_complete'))
           for r in result['candidates']):
        raise ValueError('INCOMPLETE_COVERAGE_AUTHORIZED_BUY')
    if result['refresh_stages'].get('financials',{}).get('status')=='UNAVAILABLE':raise ValueError('REAL_FINANCIAL_STAGE_FAILED')
    if result['refresh_stages'].get('calendar',{}).get('status')=='UNAVAILABLE':raise ValueError('CALENDAR_VALIDATION_FAILED')
    if not result['collected_bundle_count']:
        raise ValueError('REAL_FINANCIAL_VALIDATION_FAILED:'+json.dumps(result['refresh_stages'].get('financials')))
    if result['refresh_stages'].get('archive',{}).get('status')=='UNAVAILABLE' and not (
            result['archive_inputs_count'] and result['refresh_stages']['archive'].get('error_code')=='NEWS_REFRESH_COOLDOWN'):
        raise ValueError('REAL_NEWS_VALIDATION_FAILED:'+json.dumps(result['refresh_stages']['archive']))
    (backup/'validation.json').write_text(output);os.chmod(backup/'validation.json',0o600)
    print(output)
def create_replacement(old,name,image):
    cfg,hc=old['Config'],old['HostConfig']
    args=['docker','create','--name',name,'--user',cfg['User'],'--workdir',cfg['WorkingDir'],'--restart',hc['RestartPolicy']['Name']]
    if hc['ReadonlyRootfs']:args+=['--read-only']
    for env in cfg['Env']:args+=['-e',env]
    args+=['-e','STOCK_UNIFIED_ENABLED=true','-e','STOCK_UNIFIED_RESEARCH_DATABASE=/var/lib/stock/unified-research-inputs.sqlite3']
    for k,v in cfg.get('Labels',{}).items():args+=['--label',k+'='+v]
    for mount in old['Mounts']:args+=['--mount','type=bind,src='+mount['Source']+',dst='+mount['Destination']+('' if mount['RW'] else ',readonly')]
    for k,v in hc.get('Tmpfs',{}).items():args+=['--tmpfs',k+':'+v]
    networks=list(old['NetworkSettings']['Networks'])
    if len(networks)!=1:raise ValueError('UNEXPECTED_NETWORK_COUNT')
    args+=['--network',networks[0]]
    for alias in old['NetworkSettings']['Networks'][networks[0]].get('Aliases') or []:
        if alias not in {old['Id'],old['Id'][:12]}:args+=['--network-alias',alias]
    for field,flag in [('CapDrop','--cap-drop'),('CapAdd','--cap-add'),('SecurityOpt','--security-opt'),('GroupAdd','--group-add')]:
        for value in hc.get(field) or []:args += [flag,str(value)]
    if hc.get('Init'):args+=['--init']
    if hc.get('Memory'):args+=['--memory',str(hc['Memory'])]
    if hc.get('PidsLimit') and hc['PidsLimit']>0:args+=['--pids-limit',str(hc['PidsLimit'])]
    for port,bindings in hc.get('PortBindings',{}).items():
        for binding in bindings:args+=['-p',binding['HostIp']+':'+binding['HostPort']+':'+port]
    health=cfg.get('Healthcheck',{})
    test=health.get('Test') or []
    if test and test[0] in {'CMD','CMD-SHELL'}:
        import shlex
        command=shlex.join(test[1:]) if test[0]=='CMD' else test[1]
        args+=['--health-cmd',command,'--health-retries',str(health.get('Retries',3))]
        for key,flag in [('Interval','--health-interval'),('Timeout','--health-timeout'),('StartPeriod','--health-start-period')]:
            if key in health:args+=[flag,str(health[key])+'ns']
    elif test and test[0]=='NONE':args+=['--no-healthcheck']
    elif test:raise ValueError('UNSUPPORTED_HEALTHCHECK_CONTRACT')
    log=hc.get('LogConfig',{})
    if log.get('Type'):args+=['--log-driver',log['Type']]
    for k,v in log.get('Config',{}).items():args+=['--log-opt',k+'='+v]
    entry=cfg.get('Entrypoint') or []
    if entry:args+=['--entrypoint',entry[0]]
    args+=[image]+entry[1:]+(cfg.get('Cmd') or [])
    return run(args).strip()

def repair_health(stamp):
    release,backup=paths(stamp)
    current=inspect(CONTAINER)
    if current['Config']['Image']!=(backup/'new-image').read_text() or not current['State']['Running']:
        raise ValueError('HEALTH_REPAIR_TARGET_CHANGED')
    if current['Config'].get('Healthcheck'):
        raise ValueError('HEALTHCHECK_ALREADY_CONFIGURED')
    processes=run(['docker','top',CONTAINER])
    if any(word in processes for word in ['refresh-unified','refresh-daily','refresh-intraday','enrich-dart']):
        raise ValueError('COLLECTOR_BUSY_RETRY_WHEN_IDLE')
    prior=json.loads((backup/'container.json').read_text())
    health=prior['Config'].get('Healthcheck')
    if not health or health.get('Test',[None])[0] not in {'CMD','CMD-SHELL'}:
        raise ValueError('VERIFIED_HEALTHCHECK_BACKUP_REQUIRED')
    repair_backup=backup/'before-health-repair.json'
    if repair_backup.exists():raise ValueError('HEALTH_REPAIR_ALREADY_ATTEMPTED')
    repair_backup.write_text(json.dumps(current));os.chmod(repair_backup,0o600)
    current['Config']['Healthcheck']=health
    old_name='stock-assistant-health-rollback-'+stamp.lower()
    run(['docker','stop',CONTAINER]);run(['docker','rename',CONTAINER,old_name])
    try:
        create_replacement(current,CONTAINER,current['Config']['Image']);run(['docker','start',CONTAINER])
        for _ in range(45):
            fresh=inspect(CONTAINER)
            status=fresh['State'].get('Health',{}).get('Status')
            if status=='healthy':break
            if status=='unhealthy':raise ValueError('RESTORED_HEALTHCHECK_FAILED')
            time.sleep(1)
        else:raise ValueError('RESTORED_HEALTHCHECK_TIMEOUT')
        if fresh['HostConfig']['PortBindings']!=current['HostConfig']['PortBindings']:
            raise ValueError('HEALTH_REPAIR_PORTS_CHANGED')
        print(json.dumps({'status':'HEALTHCHECK_RESTORED','health':status,'restart_count':fresh['RestartCount'],
                          'image':fresh['Config']['Image'],'orders_enabled':False}))
    except Exception:
        if subprocess.run(['docker','inspect',CONTAINER],capture_output=True).returncode==0:
            run(['docker','stop',CONTAINER]);run(['docker','rename',CONTAINER,'stock-assistant-health-failed-'+stamp.lower()])
        run(['docker','rename',old_name,CONTAINER]);run(['docker','start',CONTAINER]);raise
def rollback(stamp):
    release,backup=paths(stamp);old_name='stock-assistant-rollback-'+stamp.lower()
    # Keep the failed candidate for inspection; no container or source deletion.
    if subprocess.run(['docker','inspect',old_name],capture_output=True).returncode==0:
        if subprocess.run(['docker','inspect',CONTAINER],capture_output=True).returncode==0:
            run(['docker','stop',CONTAINER]);run(['docker','rename',CONTAINER,'stock-assistant-failed-'+stamp.lower()])
        run(['docker','rename',old_name,CONTAINER]);run(['docker','start',CONTAINER])
    pointer=ROOT/'current';temp=ROOT/('current-rollback-'+stamp)
    temp.symlink_to((backup/'previous-current').read_text());os.replace(temp,pointer)
    for item in json.loads((backup/'files.json').read_text()):
        path=Path(item['path']);atomic(path,Path(item['backup']).read_bytes(),item['mode']);os.chown(path,item['uid'],item['gid'])
    print(json.dumps({'status':'ROLLED_BACK','backup':str(backup)}))
def deploy(stamp):
    release,backup=paths(stamp)
    if not (backup/'validation.json').exists():raise ValueError('REAL_VALIDATION_REQUIRED')
    validation=json.loads((backup/'validation.json').read_text())
    if (validation.get('contract')!='UNIFIED_RESEARCH_V1' or validation.get('policy_version')!='unified-research-2026-10-14.1'
            or validation.get('thresholds')!={'financial':70,'total':75}
            or validation.get('orders_enabled') is not False or validation.get('paper_account_eligible') is not False
            or not validation.get('collected_bundle_count')
            or validation.get('refresh_stages',{}).get('financials',{}).get('status')=='UNAVAILABLE'
            or validation.get('refresh_stages',{}).get('calendar',{}).get('status')=='UNAVAILABLE'):
        raise ValueError('SUCCESSFUL_REAL_VALIDATION_REQUIRED')
    old=json.loads((backup/'container.json').read_text())
    current=inspect(CONTAINER)
    if current['Id']!=old['Id'] or not current['State']['Running']:raise ValueError('CONTAINER_CHANGED_SINCE_VALIDATION')
    # Wait for an existing brief intraday worker to finish instead of interrupting it.
    for _ in range(12):
        processes=run(['docker','top',CONTAINER])
        if not any(x in processes for x in ['refresh-intraday','refresh-daily','refresh-unified','enrich-dart']):break
        time.sleep(5)
    else:raise ValueError('COLLECTOR_BUSY_RETRY_WHEN_IDLE')
    old_name='stock-assistant-rollback-'+stamp.lower()
    image=(backup/'new-image').read_text()
    run(['docker','stop',CONTAINER]);run(['docker','rename',CONTAINER,old_name])
    try:
        create_replacement(old,CONTAINER,image);run(['docker','start',CONTAINER])
        for _ in range(30):
            try:
                h=json.load(urllib.request.urlopen('http://127.0.0.1:9120/health',timeout=2))
                if h.get('status')=='ok' and h.get('orders_enabled') is False:break
            except Exception:pass
            time.sleep(1)
        else:raise ValueError('NEW_SERVICE_HEALTH_FAILED')
        pointer=ROOT/'current';temp=ROOT/('current-pending-'+stamp)
        if temp.exists() or temp.is_symlink():raise ValueError('UNEXPECTED_POINTER')
        temp.symlink_to(release);os.replace(temp,pointer)
        schedule=ROOT/'schedule.env'
        text=schedule.read_text()
        if not any(line.startswith('STOCK_UNIFIED_ENABLED=') for line in text.splitlines()):text+='\nSTOCK_UNIFIED_ENABLED=true\n'
        else:text='\n'.join('STOCK_UNIFIED_ENABLED=true' if line.startswith('STOCK_UNIFIED_ENABLED=') else line for line in text.splitlines())+'\n'
        atomic(schedule,text.encode())
        for relative,target in [('hermes/profiles/stock-cio/SOUL.md',PROFILE/'SOUL.md'),('hermes/skills/stock-research/SKILL.md',PROFILE/'skills/stock-research/SKILL.md')]:
            atomic(target,(release/relative).read_bytes())
        cron=PROFILE/'cron/jobs.json'
        with open(PROFILE/'cron/.jobs.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=json.loads(cron.read_text());jobs=data if isinstance(data,list) else data['jobs']
            for job in jobs:
                if job.get('name') in {'stock-morning-kst','stock-evening-kst','stock-weekly-kst'}:
                    job['no_agent']=True
                    job['prompt']='스크립트가 출력한 UNIFIED_RESEARCH_V1 보고서만 그대로 Telegram에 한 번 전달하라. 보고서 ID·기준시각, 한 개의 최대 5종목 목록, 재무 F·뉴스 N·위험 R·총점 S와 action_decision을 보존하라. BUY_RECOMMENDATION은 매수 추천 (다음 거래일 조건부), WATCH는 관찰, VERIFY_PENDING은 확인 대기, EXCLUDED는 제외로 표시한다. 모든 종목을 매수 보류로 바꾸지 않는다. 추가 기사 후보·전문가 호출·새 수집·새 점수 계산·주문·모의계좌·WIKI 쓰기는 하지 않는다. 검색 범위와 미완료 자료를 보존하라.'
                if job.get('name')=='stock-intraday-alerts-kst':job['enabled']=False
            atomic(cron,json.dumps(data,ensure_ascii=False,indent=2).encode())
        fresh=inspect(CONTAINER)
        def mount_contract(container):
            return sorted((m['Type'],m['Source'],m['Destination'],m['RW'],m.get('Propagation')) for m in container['Mounts'])
        if mount_contract(fresh)!=mount_contract(old) or fresh['HostConfig']['PortBindings']!=old['HostConfig']['PortBindings']:
            raise ValueError('RUNTIME_MOUNTS_OR_PORTS_CHANGED')
        result={'status':'DEPLOYED','image':image,'release':str(release),'backup':str(backup),
                'restart_count':fresh['RestartCount'],'orders_enabled':False,'intraday_telegram_enabled':False}
        (backup/'deployment.json').write_text(json.dumps(result));print(json.dumps(result))
    except Exception:
        rollback(stamp);raise

if __name__=='__main__':
    if os.geteuid()!=0:raise ValueError('ROOT_REQUIRED')
    mode,stamp=sys.argv[1:3]
    {'stage':lambda:stage(stamp,sys.argv[3]),'validate':lambda:validate(stamp),'deploy':lambda:deploy(stamp),'rollback':lambda:rollback(stamp),
     'repair-health':lambda:repair_health(stamp)}[mode]()
