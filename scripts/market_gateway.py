"""Isolated public data service: no source DB, account endpoint or order endpoint."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
import market_read_actor as market
from stock_assistant.providers.http import AuthenticationError

AUTH_DIR=Path('/run/secrets')

def auth_status():
    try:
        token=AUTH_DIR/'dashboard-toss-access-token'
        if token.is_file() and token.read_text().strip():
            return 'AUTH_REGISTERED_NOT_VERIFIED'
        cid=(AUTH_DIR/'dashboard-toss-client-id').read_text().strip()
        secret=(AUTH_DIR/'dashboard-toss-client-secret').read_text().strip()
        operating=(AUTH_DIR/'toss-client-id').read_text().strip()
        if not cid or not secret:
            return 'AUTH_NOT_REGISTERED'
        return 'SHARED_OPERATING_CLIENT_FORBIDDEN' if cid==operating else 'AUTH_REGISTERED_NOT_VERIFIED'
    except (OSError,UnicodeError):
        return 'AUTH_NOT_REGISTERED'

class PublicDataServer(ThreadingHTTPServer):
    def __init__(self,address,auth_status_provider=auth_status):
        self.auth_status=auth_status_provider
        self.market_lock=threading.Lock()
        self.last_success=None
        self.last_error=None
        self.last_error_status=None
        super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    server: PublicDataServer
    def setup(self):
        super().setup()
        self.connection.settimeout(5)
    def log_message(self,*args):pass
    def response(self,status,payload):
        raw=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(raw)
    def allowed(self):
        # SSH host reader only: no browser Origin or public host names accepted.
        return self.headers.get('Host') in {'127.0.0.1:9130','localhost:9130'} and not self.headers.get('Origin')
    def do_GET(self):
        if not self.allowed():return self.response(403,{'error':'LOCAL_HOST_READER_REQUIRED'})
        if self.path!='/health':return self.response(404,{'error':'NOT_FOUND'})
        status=self.server.auth_status()
        if status=='AUTH_REGISTERED_NOT_VERIFIED':
            status='PUBLIC_DATA_ERROR' if self.server.last_error else ('AUTH_VERIFIED' if self.server.last_success else status)
        self.response(200,{'status':status,
                           'service':'ISOLATED_PUBLIC_DATA','orders_enabled':False,'production_db_changed':False,
                           'last_success_at':self.server.last_success,'last_error':self.server.last_error,
                           'upstream_http_status':self.server.last_error_status})
    def do_POST(self):
        if not self.allowed():return self.response(403,{'error':'LOCAL_HOST_READER_REQUIRED'})
        if self.path!='/public-market':return self.response(404,{'error':'NOT_FOUND'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2048:raise ValueError('body')
            request=json.loads(self.rfile.read(length))
            if not isinstance(request,dict) or set(request)-{'operation','symbols','mode'} or request.get('operation')!='public_market' or request.get('mode','DIRECT')!='DIRECT':
                raise ValueError('operation')
            symbols=market.validate_symbols(request.get('symbols'))
        except (ValueError,TypeError,KeyError):
            return self.response(400,{'error':'INVALID_PUBLIC_REQUEST'})
        status=self.server.auth_status()
        if status!='AUTH_REGISTERED_NOT_VERIFIED':
            return self.response(503,{'error':status,'quotes':[],'production_db_changed':False})
        if not self.server.market_lock.acquire(timeout=1):
            return self.response(503,{'error':'PUBLIC_READER_BUSY','quotes':[]})
        try:
            result=market.direct(symbols)
            now=datetime.now(timezone.utc).isoformat()
            result['retrieved_at']=now
            self.server.last_success=now
            self.server.last_error=None
            self.server.last_error_status=None
            self.response(200,result)
        except Exception as exc:
            self.server.last_error=type(exc).__name__
            status=getattr(exc.__cause__,'code',None)
            self.server.last_error_status=status if isinstance(status,int) and 400<=status<=599 else None
            supplied=isinstance(market.client,market.SuppliedAccessTokenClient)
            if isinstance(exc,AuthenticationError) and hasattr(market.client,'mark_rejected'):market.client.mark_rejected()
            if market.client is not None and isinstance(exc,AuthenticationError) and not supplied:market.client._access_token=None
            # Upstream response bodies, credentials and exception text never leave.
            error='SUPPLIED_ACCESS_TOKEN_REJECTED_OR_EXPIRED' if supplied and isinstance(exc,AuthenticationError) else 'PUBLIC_DATA_'+type(exc).__name__
            self.response(502,{'error':error,'upstream_http_status':self.server.last_error_status,'quotes':[],'production_db_changed':False})
        finally:self.server.market_lock.release()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--bind',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=9130)
    args=parser.parse_args()
    server=PublicDataServer((args.bind,args.port))
    try:server.serve_forever()
    finally:server.server_close()
