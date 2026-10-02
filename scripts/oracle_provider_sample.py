"""Five KRX date/market reads and one DART year read; keys never leave Oracle."""
import hashlib
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

result={"checked_at_kst":datetime.now(timezone(timedelta(hours=9))).isoformat(),"scope":"bounded provider sample; no production database access or writes","observed_at":datetime.now(timezone.utc).isoformat(),"krx":[],"dart":{},"price_rows":[],"three_year_certified":False}
try:
    from stock_assistant.providers.krx import KrxClient
    from stock_assistant.providers.dart import DartClient, DART_FINANCIAL_URL
    from stock_assistant.models import to_json_value
    krx=KrxClient(Path('/run/secrets/krx-auth-key').read_text().strip())
    symbols={'240810','054920','044450','053800','062040'}
    for market,day in [('KOSPI',date(2023,11,1)),('KOSDAQ',date(2023,11,1)),('KOSPI',date(2026,9,29)),('KOSDAQ',date(2026,9,29)),('ETF',date(2026,9,29))]:
        item={"market":market,"date":day.isoformat()}
        try:
            snapshot=krx.daily_snapshot(market,day,observed_at=datetime.fromisoformat(result['observed_at']))
            item.update(status='NORMALIZED',bars=len(snapshot.bars),symbols=len({b.symbol for b in snapshot.bars}))
            result['price_rows']+=to_json_value([b for b in snapshot.bars if b.symbol in symbols])
        except Exception as exc:
            item.update(status='UNAVAILABLE',error_type=type(exc).__name__)
        result['krx'].append(item)
    dart=DartClient(Path('/run/secrets/dart-api-key').read_text().strip())
    try:
        codes=dart.corp_codes()
        corp=codes.get('240810')
        if not corp:
            result['dart']={'status':'CORP_CODE_UNAVAILABLE'}
        else:
            response=dart.fetch_json(DART_FINANCIAL_URL,query={'crtfc_key':dart.api_key,'corp_code':corp,'bsns_year':'2023','reprt_code':'11011','fs_div':'CFS'})
            payload=response.payload
            rows=payload.get('list',[])
            result['dart']={"status_code":payload.get('status'),"business_year":2023,"report_code":"11011","division":"CFS","rows":len(rows),"receipt_numbers":sorted({r.get('rcept_no') for r in rows if r.get('rcept_no')}),"historical_version_certified":False,"limitation":"API fiscal-year query does not prove original contemporaneous revision"}
    except Exception as exc:
        result['dart']={'status':'UNAVAILABLE','error_type':type(exc).__name__}
except Exception as exc:
    result['initialization_error_type']=type(exc).__name__
result['sample_dataset_id']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
print(json.dumps(result,ensure_ascii=False,indent=2))

