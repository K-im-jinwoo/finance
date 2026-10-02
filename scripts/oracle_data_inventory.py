"""Execute inside existing stock container. SQL read-only; no holdings/secret output."""
import json
import sqlite3
import urllib.request
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

result={"checked_at_kst":datetime.now(timezone(timedelta(hours=9))).isoformat(),"database":"/var/lib/stock/stock-assistant.sqlite3","read_only":True,"production_db_changed":False,"secret_presence_only":{},"tables":{},"point_in_time_certified":False}
for name in ('krx-auth-key','dart-api-key'):
    path=Path('/run/secrets')/name
    try:
        result['secret_presence_only'][name]={"exists":path.is_file(),"nonempty":bool(path.read_text().strip())}
    except (OSError,UnicodeError):
        result['secret_presence_only'][name]={"readable":False}
try:
    result['health']=json.load(urllib.request.urlopen('http://127.0.0.1:9120/health',timeout=5))
except Exception as exc:
    result['health']={"error_type":type(exc).__name__}
path=Path(result['database'])
with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
    c.execute('PRAGMA query_only=ON')
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    allowed={'ohlcv':'trade_date','financial_snapshots':'period_end','financial_company_snapshots':'period_end','etf_snapshots':'trade_date','financing_events':'announced_at','catalysts':'announced_at','management_risks':'published_at','research_coverage':'start_date','reports':'as_of','performance_records':'evaluated_at','securities':None}
    for table,period in allowed.items():
        if table not in tables:
            result['tables'][table]={"state":"MISSING_TABLE"}
            continue
        columns={r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}
        item={'rows':c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]}
        for field in (period,'observed_at','published_at','end_date'):
            if field and field in columns:
                item[field+'_range']=list(c.execute(f'SELECT MIN("{field}"),MAX("{field}") FROM "{table}"').fetchone())
        if 'symbol' in columns:
            item['symbols']=c.execute(f'SELECT COUNT(DISTINCT symbol) FROM "{table}"').fetchone()[0]
        if table=='ohlcv':
            item['distinct_dates']=c.execute('SELECT COUNT(DISTINCT trade_date) FROM ohlcv').fetchone()[0]
            item['daily_rows']=[{'date':r[0],'symbols':r[1],'first_observed_at':r[2],'last_observed_at':r[3]} for r in c.execute('SELECT trade_date,COUNT(DISTINCT symbol),MIN(observed_at),MAX(observed_at) FROM ohlcv GROUP BY trade_date ORDER BY trade_date')]
            item['symbol_period_counts']=[{'start':r[0],'end':r[1],'distinct_dates':r[2],'symbols':r[3]} for r in c.execute('SELECT first_date,last_date,days,COUNT(*) FROM (SELECT symbol,MIN(trade_date) AS first_date,MAX(trade_date) AS last_date,COUNT(DISTINCT trade_date) AS days FROM ohlcv GROUP BY symbol) GROUP BY first_date,last_date,days ORDER BY first_date,days DESC')]
        if table=='research_coverage':
            item['dataset_windows']=[{'dataset':r[0],'start':r[1],'end':r[2],'observed_start':r[3],'observed_end':r[4],'symbols':r[5]} for r in c.execute('SELECT dataset,MIN(start_date),MAX(end_date),MIN(observed_at),MAX(observed_at),COUNT(DISTINCT symbol) FROM research_coverage GROUP BY dataset')]
        if table=='financial_snapshots':
            item['observed_at_column']='observed_at' in columns
            rows=[json.loads(r[0]) for r in c.execute('SELECT payload_json FROM financial_snapshots')]
            item['fields_present_count']={key:sum(p.get(key) is not None for p in rows) for key in ('annual_operating_income','ttm_operating_income','ttm_period_end','ttm_source_url','operating_cash_flow','observed_at','revision_id')}
            item['ttm_period_range']=sorted({p['ttm_period_end'] for p in rows if p.get('ttm_period_end')})
        if table=='performance_records':
            item['status_counts']={r[0]:r[1] for r in c.execute('SELECT status,COUNT(*) FROM performance_records GROUP BY status')}
        if table=='securities':
            item['sample_security_metadata']=[{key:payload.get(key) for key in ('symbol','name','market','asset_type','company_kind','listed_on','delisted_on')} for payload in [json.loads(row[0]) for row in c.execute("SELECT payload_json FROM securities WHERE symbol IN ('240810','054920','044450','053800','062040') ORDER BY symbol")]]
        result['tables'][table]=item
print(json.dumps(result,ensure_ascii=False,indent=2))

