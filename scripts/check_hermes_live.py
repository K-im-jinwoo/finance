"""Verify the running loopback account without controls, remote writes or secrets."""
import hashlib
import http.cookiejar
import json
import urllib.request
from bootstrap import ROOT
from research_dashboard.live_accounts import utc_now

if __name__=='__main__':
    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    client.open('http://127.0.0.1:8765/hermes',timeout=5).read()
    def get(path):return json.load(client.open('http://127.0.0.1:8765'+path,timeout=5))
    source=get('/api/paper-accounts')
    aid=source['accounts'][0]
    result=get('/api/paper-accounts/'+aid)
    latest=result['reports'][0]
    assert latest['source']=='HERMES_STOCK_CIO_FINAL_MESSAGE'
    assert latest['selection_status']=='EXPLICIT_RANK_1'
    assert hashlib.sha256(latest['content'].encode()).hexdigest()==latest['content_sha256']
    assert latest['selected']['rank']==1
    assert not result['state']['trades'], 'Opening fills require the separate live data gate'
    assert set(result['state']['pending'])=={latest['selected']['symbol']}
    assert result['state']['pending'][latest['selected']['symbol']]['created_at']>=result['created_at']
    assert source['source']['state']=='CONNECTED'
    assert source['source']['mode'] in {'OPERATING_STORE_READ_ONLY','DEDICATED_PUBLIC_REST'}
    if source['source']['mode']=='DEDICATED_PUBLIC_REST':
        assert result['meta']['feed']['auth_mode']=='SHARED_CLIENT_TOKEN_CACHE'
        assert result['meta']['feed']['source_update_seconds']==30
        assert result['meta']['feed']['execution_gate']=='OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED'
        assert result['meta']['calendar']['source']=='TOSS_SECURITIES_OPEN_API'
    assert not source['source']['production_db_changed']
    repeated=get('/api/paper-accounts/'+aid)
    assert repeated['state']['trades']==result['state']['trades']
    ui_path=ROOT/('artifacts/hermes-shared-auth-ui-verification.json' if source['source']['mode']=='DEDICATED_PUBLIC_REST' else 'artifacts/hermes-ui-verification.json')
    ui=json.loads(ui_path.read_text(encoding='utf-8')) if ui_path.exists() else None
    if ui:
        assert ui['original_sha256']==latest['content_sha256']
        assert not ui['error'] and ui['scrollWidth']<=ui['width']
        assert ui['mobile']['scrollWidth']<=ui['mobile']['width'] and ui['viewport_restored']
    record={'checked_at':utc_now(),'account_id':aid,'created_at':result['created_at'],'source_status':source['source'],
            'report_id':latest['report_id'],'source':latest['source'],'original_sha256':latest['content_sha256'],
            'original_hash_equal':True,'selected_symbol':latest['selected']['symbol'],'selected_rank':1,
            'original_decision':latest['selected']['decision'],'reports_available':len(result['reports']),
            'virtual_fills':len(result['state']['trades']),'pending_symbols':list(result['state']['pending']),
            'quote':result['watch_quotes'].get(latest['selected']['symbol']),
            'original_experiment_preserved':any(e['experiment_id']=='E-753020984c45aa6a99387f4a' for e in get('/api/experiments')),
            'live_tick_verified':False,'market_open_fill_verified':False,'production_db_changed':False,
            'browser_original_hash_equal':ui is not None,'pc_and_mobile_no_page_overflow':ui is not None}
    (ROOT/'artifacts/hermes-live-verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'source_status':record['source_status']['state'],'selected_symbol':record['selected_symbol'],'reports':record['reports_available'],'fills':record['virtual_fills'],'hash_equal':True,'original_experiment_preserved':record['original_experiment_preserved']}))
