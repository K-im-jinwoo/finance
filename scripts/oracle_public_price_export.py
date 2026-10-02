"""Export selected public OHLCV only, not a database/holdings/report backup."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

symbols=['240810','054920','044450','053800','062040']
result={'schema_version':1,'purpose':'OBSERVED_PUBLIC_PRICE_CALCULATION_VALIDATION','retrieved_at':datetime.now(timezone.utc).isoformat(),'source_observation_preserved':True,'source_database_changed':False,'historical_point_in_time_certified':False,'rows':[]}
with closing(sqlite3.connect(Path('/var/lib/stock/stock-assistant.sqlite3').as_uri()+'?mode=ro',uri=True)) as c:
    c.execute('PRAGMA query_only=ON')
    for (raw,) in c.execute('SELECT payload_json FROM ohlcv WHERE symbol IN (?,?,?,?,?) ORDER BY symbol,trade_date,observed_at',symbols):
        payload=json.loads(raw)
        result['rows'].append({k:payload[k] for k in ('symbol','trade_date','open','high','low','close','volume','source','observed_at')})
print(json.dumps(result,ensure_ascii=False,indent=2))

