import hashlib
import json
import os
import subprocess
from pathlib import Path
from bootstrap import ROOT
from research_dashboard.live_accounts import utc_now

if __name__=='__main__':
    source=ROOT.parent/'wiki/.work/stock-investment-assistant'
    manifest=json.loads((ROOT/'artifacts/source-audit.json').read_text(encoding='utf-8'))
    env={**os.environ,'GIT_OPTIONAL_LOCKS':'0'}
    def git(*args):return subprocess.check_output(['git','-c','safe.directory='+str(source),'-C',str(source),*args],text=True,encoding='utf-8',env=env).splitlines()
    mismatch=[r['path'] for r in manifest['snapshot_files'] if hashlib.sha256((source/r['path']).read_bytes()).hexdigest()!=r['sha256']]
    documents=[]
    for relative in ('docs/reports/2026-09-30-stock-research-dashboard-handoff-report.md','docs/superpowers/plans/2026-09-30-stock-research-dashboard-handoff-plan.md'):
        documents.append({'path':relative,'match':(source/relative).read_bytes()==(ROOT/'docs/reference'/Path(relative).name).read_bytes()})
    result={'checked_at':utc_now(),'original_head':git('rev-parse','HEAD')[0],'source_status_unchanged':git('status','--short')==manifest['source_status'],
            'source_files':len(manifest['snapshot_files']),'mismatch_files':mismatch,'preserved_documents':documents}
    assert result['original_head']==manifest['source_head'] and result['source_status_unchanged'] and not mismatch and all(d['match'] for d in documents)
    (ROOT/'artifacts/hermes-source-preservation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'source_files':result['source_files'],'source_status_unchanged':True,'original_documents_equal':True}))
