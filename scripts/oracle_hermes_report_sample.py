"""Read final stock-CIO report messages and cron output metadata; no agent invocation."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

root=Path('/home/ubuntu/.hermes/profiles/stock-cio')
result={'retrieved_at':datetime.now(timezone.utc).isoformat(),'source':'HERMES_STOCK_CIO_FINAL_MESSAGE',
        'read_only':True,'production_db_changed':False,'messages':[],'cron_outputs':[]}
with closing(sqlite3.connect((root/'state.db').as_uri()+'?mode=ro',uri=True)) as connection:
    connection.execute('PRAGMA query_only=ON')
    query="""SELECT id,session_id,timestamp,content,finish_reason FROM messages
             WHERE role='assistant' AND (tool_calls IS NULL OR tool_calls='' OR tool_calls='[]')
             AND (content LIKE '%CIO 최종판정%' OR content LIKE '%CIO 최종 판정%' OR content LIKE '%1순위%')
             ORDER BY timestamp DESC,id DESC LIMIT 4"""
    for row in connection.execute(query):
        item={'message_id':row[0],'session_ref':hashlib.sha256(str(row[1]).encode()).hexdigest()[:20],
              'timestamp':row[2],'content':row[3],'finish_reason':row[4],
              'content_sha256':hashlib.sha256((row[3] or '').encode()).hexdigest(),
              'report_ids':sorted(set(re.findall(r'R-\d{8}T\d{4}Z-[A-F0-9]{8}',row[3] or '')))}
        result['messages'].append(item)
folder=root/'cron/output'
if folder.is_dir():
    paths=sorted([p for p in folder.glob('*/*') if p.is_file()],key=lambda p:p.stat().st_mtime,reverse=True)[:8]
    result['cron_outputs']=[{'relative_path':str(path.relative_to(root)),'bytes':path.stat().st_size,
                            'modified_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()} for path in paths]
print(json.dumps(result,ensure_ascii=False,indent=2))
