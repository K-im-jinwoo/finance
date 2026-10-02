"""Fixed local Oracle endpoint over SSH. No credentials transmitted to the PC."""
import json
import re
import sys
import urllib.request
from urllib.error import HTTPError

for line in sys.stdin:
    try:
        body=json.loads(line)
        symbols=body.get('symbols')
        if body.get('operation')!='public_market' or body.get('mode')!='DIRECT' or not isinstance(symbols,list) or not 1<=len(symbols)<=6 or not all(isinstance(s,str) and re.fullmatch(r'\d{6}',s) for s in symbols) or len(set(symbols))!=len(symbols):
            raise ValueError('invalid public request')
        request=urllib.request.Request('http://127.0.0.1:9130/public-market',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
        try:
            response=urllib.request.urlopen(request,timeout=18)
        except HTTPError as exc:response=exc
        with response:
            raw=response.read(1_000_001)
            if len(raw)>1_000_000:raise ValueError('bounded response required')
            result=json.loads(raw)
    except Exception as exc:
        result={'error':'PUBLIC_GATEWAY_'+type(exc).__name__,'quotes':[],'production_db_changed':False}
    print(json.dumps(result,ensure_ascii=False),flush=True)
