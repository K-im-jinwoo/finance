"""Public data only; never instantiate the migrating production repository.

Default STORE mode cannot issue tokens or write any remote DB. The approved
shared gateway injects one coordinated client/cache for DIRECT. Standalone
fallbacks accept a supplied token or separate credentials and reject unmanaged
independent issuance for the operating collector's ID.
"""
import json
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

client = None
calendar_cache = None

class SuppliedAccessTokenClient:
    """Only public GET requests. No Client Secret, token issuance or refresh path."""
    def __init__(self,path):
        self.path=path
        self._last_used_token=None
        self._rejected_token=None
    def mark_rejected(self):self._rejected_token=self._last_used_token
    def _token(self):
        token=self.path.read_text().strip()
        if not 1<=len(token)<=8192 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token):
            raise ValueError('INVALID_SUPPLIED_ACCESS_TOKEN')
        if token==self._rejected_token:
            from stock_assistant.providers.http import AuthenticationError
            raise AuthenticationError('supplied access token requires replacement')
        self._last_used_token=token
        return token
    def prices(self,symbols,*,observed_at):
        from stock_assistant.providers.toss import TOSS_BASE_URL, normalize_toss_prices
        from stock_assistant.providers.http import get_json, UpstreamSchemaError
        response=get_json(TOSS_BASE_URL+'/api/v1/prices',query={'symbols':','.join(symbols)},
                          headers={'Authorization':'Bearer '+self._token()})
        quotes=normalize_toss_prices(response.payload,observed_at=max(observed_at,datetime.now(timezone.utc)))
        if {quote.symbol for quote in quotes}!=set(symbols):
            raise UpstreamSchemaError('public prices do not match requested symbols')
        return quotes

def auth_mode():
    path=Path('/run/secrets/dashboard-toss-access-token')
    return 'SUPPLIED_ACCESS_TOKEN' if path.is_file() and path.read_text().strip() else 'SEPARATE_CLIENT_CREDENTIALS'

def read_store(symbols):
    quotes = []
    path = Path('/var/lib/stock/stock-assistant.sqlite3')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA query_only=ON')
        for symbol in symbols:
            row = c.execute('SELECT payload_json FROM market_quotes WHERE symbol=? ORDER BY observed_at DESC LIMIT 1',(symbol,)).fetchone()
            if row:
                quotes.append(json.loads(row[0]))
    return {"quotes":quotes,"feed_mode":"OPERATING_STORE_READ_ONLY","source_update_seconds":300,
            "execution_gate":"DEDICATED_RAW_OPEN_AND_CALENDAR_CONNECTION_REQUIRED","production_db_changed":False}

def direct(symbols):
    global client, calendar_cache
    from stock_assistant.providers.toss import TossMarketDataClient, normalize_toss_candles, TOSS_BASE_URL, TOSS_SOURCE
    from stock_assistant.providers.http import get_json
    from stock_assistant.models import to_json_value
    if client is None:
        if auth_mode()=='SUPPLIED_ACCESS_TOKEN':
            client=SuppliedAccessTokenClient(Path('/run/secrets/dashboard-toss-access-token'))
        else:
            cid_path = Path('/run/secrets/dashboard-toss-client-id')
            secret_path = Path('/run/secrets/dashboard-toss-client-secret')
            if not cid_path.is_file() or not secret_path.is_file():
                raise ValueError('DEDICATED_PUBLIC_DATA_AUTH_REQUIRED')
            cid = cid_path.read_text().strip()
            if cid == Path('/run/secrets/toss-client-id').read_text().strip():
                raise ValueError('SHARED_OPERATING_TOKEN_FORBIDDEN')
            client = TossMarketDataClient(cid,secret_path.read_text().strip())
    now = datetime.now(timezone.utc)
    local_day = now.astimezone(timezone(timedelta(hours=9))).date().isoformat()
    # Cached token stays inside this remote process. No credential/token stdout.
    headers = {"Authorization":"Bearer "+client._token()}
    if calendar_cache is None or calendar_cache['date'] != local_day:
        payload = get_json(TOSS_BASE_URL+'/api/v1/market-calendar/KR',query={"date":local_day},headers=headers).payload['result']
        today = payload['today']
        integrated = today.get('integrated') or {}
        regular = integrated.get('regularMarket')
        nxt = (payload.get('nextBusinessDay') or {}).get('integrated') or {}
        calendar_cache = {"date":local_day,"regular_open":regular['startTime'] if regular else None,
                          "regular_close":regular['endTime'] if regular else None,
                          "next_regular_open":(nxt.get('regularMarket') or {}).get('startTime'),
                          "observed_at":datetime.now(timezone.utc).isoformat(),"source":TOSS_SOURCE}
    response = {"quotes":to_json_value(client.prices(symbols,observed_at=now)),"calendar":calendar_cache,
                "feed_mode":"DEDICATED_PUBLIC_REST","source_update_seconds":30,"production_db_changed":False,
                "auth_mode":getattr(client,'auth_mode','SUPPLIED_ACCESS_TOKEN' if isinstance(client,SuppliedAccessTokenClient) else 'SEPARATE_CLIENT_CREDENTIALS')}
    opened = calendar_cache['regular_open']
    lag = (datetime.now(timezone.utc)-datetime.fromisoformat(opened)).total_seconds() if opened else -1
    if 60 <= lag <= 300:
        end = datetime.fromisoformat(opened)+timedelta(minutes=1)
        prices, volumes = {}, {}
        for symbol in symbols:
            payload = get_json(TOSS_BASE_URL+'/api/v1/candles', query={"symbol":symbol,"interval":"1m","count":"1",
                               "before":end.isoformat(),"adjusted":"false"},headers=headers).payload
            candles = normalize_toss_candles(payload,symbol=symbol,observed_at=datetime.now(timezone.utc))
            for candle in candles:
                if candle.timestamp == end and candle.volume > 0:
                    prices[symbol], volumes[symbol] = str(candle.open), candle.volume
        response['opened'] = {"open_at":opened,"bar_end":end.isoformat(),"prices":prices,"volumes":volumes,
                              "session_id":"KR-REGULAR-"+local_day,"basis":"UNADJUSTED",
                              "observed_at":datetime.now(timezone.utc).isoformat(),"source":TOSS_SOURCE}
    return response

def validate_symbols(symbols):
    if not isinstance(symbols,list) or not 1 <= len(symbols) <= 6 or not all(isinstance(s,str) and re.fullmatch(r'\d{6}',s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('INVALID_PUBLIC_SYMBOLS')
    return symbols

def serve_stream():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get('operation') != 'public_market':
                raise ValueError('INVALID_OPERATION')
            symbols = validate_symbols(request.get('symbols'))
            mode = request.get('mode','STORE')
            if mode not in {'STORE','DIRECT'}:
                raise ValueError('INVALID_MODE')
            response = read_store(symbols) if mode == 'STORE' else direct(symbols)
            response['retrieved_at'] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            response = {"error":type(exc).__name__,"quotes":[],"production_db_changed":False}
            if client is not None:
                client._access_token = None
        print(json.dumps(response,ensure_ascii=False),flush=True)

if __name__=='__main__':
    serve_stream()
