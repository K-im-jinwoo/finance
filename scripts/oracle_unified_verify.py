"""Minimum-field verification of the explicitly approved stock rollout."""
import json
from pathlib import Path
import subprocess
import sys

from oracle_unified_rollout import SSH, HOST, OPTIONS

REMOTE = r'''
import json,sqlite3,subprocess,urllib.request,shlex
from datetime import datetime,timezone
from pathlib import Path
root=Path('/srv/stock-assistant')
profile=Path('/home/ubuntu/.hermes/profiles/stock-cio')
container=json.loads(subprocess.check_output(['docker','inspect','stock-assistant-stock-assistant-1']))[0]
health=json.load(urllib.request.urlopen('http://127.0.0.1:9120/health',timeout=10))
jobs=json.loads((profile/'cron/jobs.json').read_text())
jobs=jobs if isinstance(jobs,list) else jobs['jobs']
stock=[j for j in jobs if str(j.get('name','')).startswith('stock-')]
out={'observed_at':datetime.now(timezone.utc).isoformat(),'current':str((root/'current').resolve()),'image':container['Config']['Image'],
     'running':container['State']['Running'],'health':container['State'].get('Health',{}).get('Status'),
     'restart_count':container['RestartCount'],'orders_enabled':health['orders_enabled'],
     'jobs':[{**{k:j.get(k) for k in ['id','name','enabled','no_agent','schedule','last_run_at','last_status']},
              'delivery_error_present':bool(j.get('last_delivery_error')),
              'delivery_unverified':bool(j.get('last_delivery_unverified')),
              'delivery_queued':bool(j.get('last_delivery_queued'))} for j in stock]}
backup=root/'backups'/(root/'current').resolve().name
if (backup/'container.json').is_file():
    baseline=json.loads((backup/'container.json').read_text())
    def mounts(value):return sorted((m['Type'],m['Source'],m['Destination'],m['RW'],m.get('Propagation')) for m in value['Mounts'])
    def environment(value):return dict(entry.split('=',1) for entry in value['Config']['Env'])
    old_env,new_env=environment(baseline),environment(container)
    for field in ['STOCK_UNIFIED_ENABLED','STOCK_UNIFIED_RESEARCH_DATABASE']:
        old_env.pop(field,None);new_env.pop(field,None)
    old_health,new_health=baseline['Config'].get('Healthcheck',{}),container['Config'].get('Healthcheck',{})
    out['runtime_preserved']={
      'mounts':mounts(container)==mounts(baseline),
      'ports':container['HostConfig']['PortBindings']==baseline['HostConfig']['PortBindings'],
      'user':container['Config']['User']==baseline['Config']['User'],
      'read_only':container['HostConfig']['ReadonlyRootfs']==baseline['HostConfig']['ReadonlyRootfs'],
      'environment':old_env==new_env,
      'healthcheck':bool(new_health) and old_health==new_health,
      'network':set(container['NetworkSettings']['Networks'])==set(baseline['NetworkSettings']['Networks'])}
for file,key in [('executions.db','executions'),('deliveries.db','deliveries')]:
    p=profile/'cron'/file
    conn=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    table='executions' if key=='executions' else 'deliveries'
    cols={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
    safe=[c for c in ['execution_id','job_id','status','started_at','created_at','finished_at','success','delivery_status','delivery_outcome','error_class'] if c in cols]
    if safe:
        rows=conn.execute('SELECT '+','.join(safe)+(' ,job_json,content' if key=='deliveries' else '')+' FROM '+table+' ORDER BY '+('created_at' if 'created_at' in cols else 'started_at')+' DESC LIMIT 12').fetchall()
        visible=[]
        for row in rows:
            record=dict(row)
            if key=='deliveries':
                job=json.loads(record.pop('job_json'));content=record.pop('content')
                if not str(job.get('name','')).startswith('stock-'):continue
                record['job_name']=job.get('name')
                import re
                record['unified_report_ids']=sorted(set(re.findall(r'UR-[a-f0-9]{24}',content)))
                record['unified_format']= 'UNIFIED_RESEARCH_V1' in content
                record['contains_legacy_hold']= '추가 검토 / 매수 보류' in content
            elif record.get('job_id') not in {j['id'] for j in stock}:continue
            visible.append(record)
        out[key]=visible
    else:out[key]={'schema_columns':sorted(cols)}
    conn.close()
# Use the existing role-token file inside the profile; never export its contents.
import sys
sys.path.insert(0,str((root/'current/src').resolve()))
from stock_assistant.client import StockClient
token=None
for env_file in [profile/'profile.env',profile/'.env']:
    if not env_file.is_file():continue
    for line in env_file.read_text().splitlines():
        if line.startswith('STOCK_API_ROLE_TOKEN_FILE='):
            token=Path(shlex.split(line.split('=',1)[1])[0])
if token is None:
    out['unified_api']='TOKEN_FILE_LOCATION_REQUIRED'
else:
    report=StockClient('http://127.0.0.1:9120',token).latest_unified_report()
    out['report']={k:report.get(k) for k in ['report_id','as_of','policy_version','policy_status','contract','thresholds','research_windows','coverage_scope','research_progress','expected_price_session','decision_counts','pending_reason_counts','orders_enabled','paper_account_eligible']}
    out['report']['candidates']=[{k:r.get(k) for k in ['symbol','action_decision','recent_news_complete','risk_history_complete']} for r in report.get('candidates',[])]
    stages=report.get('refresh_stages',{})
    financial=stages.get('financials',{})
    from collections import Counter
    out['report']['refresh_stages']={
       'financials':{**{k:financial.get(k) for k in ['selected','processed','requests','status','error_code']},
                     'result_counts':dict(Counter(r.get('status') for r in financial.get('results',[])))},
       'archive':{k:v for k,v in stages.get('archive',{}).items() if k in {'status','error_code','symbol','actual_calls','requested','results','next_start','scope'}},
       'material_archive':{k:v for k,v in stages.get('material_archive',{}).items() if k in {'status','error_code','symbol','actual_calls','query_states','scope'}},
       'news_budget':stages.get('news_budget',{}),
       'research_focus':stages.get('research_focus',{}),
       'calendar':{'verified':bool(stages.get('calendar',{}).get('input_id')),'previous_session':stages.get('calendar',{}).get('previous_session')},
       'prices':{k:stages.get('prices',{}).get(k) for k in ['status','target_date','market_dates','error_code','missing_market']},
       'official_events':{'verified_count':len(stages.get('official_events',{}).get('verified',[])),
                          'pending_count':len(stages.get('official_events',{}).get('pending',[]))}}
    outputs=sorted(p for job in ['07845c567fda','ce8483a6f7ce','5084e2eee64c'] for p in (profile/'cron/output'/job).glob('*.md'))
    outputs.sort(key=lambda p:p.name)
    if outputs:
        # Only the explicitly run stock job's newest output; export booleans, not its body.
        body=outputs[-1].read_text()
        out['telegram_output']={'report_matches_latest':report['report_id'] in body,
          'direct_script': 'no_agent (script)' in body,
          'unified_title': '통합 종목 연구 · 다음 거래일 조건부' in body,
          'score_formula': '재무 70% + 뉴스 30% - 위험 감점' in body,
          'legacy_hold_absent': '추가 검토 / 매수 보류' not in body,
          'missing_history_shown': any(value in body for value in ['NEWS_HISTORY_COVERAGE_UNAVAILABLE','3년 뉴스 검토 미완료','최근 90일 뉴스 조사 미완료','과거 3년 위험 조사 미완료'])}
    evening=sorted((profile/'cron/output/ce8483a6f7ce').glob('*.md'))
    if evening:
        import re
        body=evening[-1].read_text()
        out['evening_output']={'file_date':evening[-1].stem,
            'unavailable_krx':re.findall(r'KRX daily data unavailable: (KOSPI|KOSDAQ|ETF) (20\d{2}-\d{2}-\d{2})',body),
            'failed_stages':re.findall(r'STOCK_CYCLE status=FAILED stage=([A-Z_]+)',body),
            'report_ids':re.findall(r'UR-[a-f0-9]{24}',body)}
        out['evening_output']['structured_diagnostics']=[]
        for line in body.splitlines():
            try:p=json.loads(line)
            except (ValueError,TypeError):continue
            if isinstance(p,dict) and p.get('stage')=='DAILY_REFRESH':
                out['evening_output']['structured_diagnostics'].append({k:p.get(k) for k in ['stage','status','error_class']})
    from stock_assistant.research_data import ResearchData
    store=ResearchData(root/'state/unified-research-inputs.sqlite3',source_database=root/'state/stock-assistant.sqlite3',read_only=True)
    now=datetime.now(timezone.utc)
    out['archive_progress']=[{'symbol':s,**{k:r['payload'].get(k) for k in ['status','start','end','next_start','oldest_seen','provider_total']}}
          for s,r in store.latest_by_symbol('archive_cursor',as_of=now).items()]
    out['financial_input_status_counts']=dict(Counter(r['payload'].get('status') for r in store.latest_by_symbol('financial_bundle',as_of=now).values()))
    out['split_input_counts']={kind:len(store.latest_by_symbol(kind,as_of=now)) for kind in ['recent_archive_cursor','recent_news_coverage','risk_archive_cursor','risk_coverage']}
print(json.dumps(out,ensure_ascii=False))
'''

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    result=subprocess.run([SSH,'-T',*OPTIONS,HOST,'sudo -n python3 -'],input=REMOTE,
                          capture_output=True,text=True,encoding='utf-8',timeout=45,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError(result.stderr[-2500:])
    value=json.loads(result.stdout)
    print(json.dumps(value,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
