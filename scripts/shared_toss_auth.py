"""One client credential pair and one locked token cache across local processes.

No keys or tokens in arguments, stdout or logs. The cache is a dedicated secret
directory, unrelated to the investment database. Enable only on coordinated rollout.
"""
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from stock_assistant.providers.toss import TossMarketDataClient


class SharedTokenCache:
    def __init__(self,directory,client_id,clock=time.time):
        self.directory=Path(directory).absolute()
        self.binding=hashlib.sha256(client_id.encode()).hexdigest()
        self.clock=clock
        self.path=self.directory/'toss-token.json'

    @contextmanager
    def locked(self):
        directory=self.directory
        if directory.is_symlink() or directory.resolve()!=directory:
            raise ValueError('UNEXPECTED_AUTH_CACHE_DIRECTORY')
        directory.mkdir(mode=0o700,exist_ok=True)
        if os.name!='nt' and directory.stat().st_mode&0o077:
            raise ValueError('AUTH_CACHE_DIRECTORY_PERMISSIONS_REQUIRED')
        lock=directory/'toss-token.lock'
        if lock.is_symlink() or self.path.is_symlink():raise ValueError('UNEXPECTED_AUTH_CACHE_FILE')
        fd=os.open(lock,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'r+b') as stream:
            # Windows can lock a byte past EOF. Keep the lock file empty so two
            # first callers cannot race while initializing a byte before locking.
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                deadline=time.monotonic()+30
                while True:
                    try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1);break
                    except OSError:
                        if time.monotonic()>=deadline:raise TimeoutError('SHARED_AUTH_CACHE_BUSY') from None
                        time.sleep(0.05)
            else:
                import fcntl
                deadline=time.monotonic()+30
                while True:
                    try:fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=deadline:raise TimeoutError('SHARED_AUTH_CACHE_BUSY') from None
                        time.sleep(0.05)
            try:yield
            finally:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():return None
        if self.path.stat().st_size>16384:raise ValueError('SHARED_AUTH_CACHE_INVALID')
        if os.name!='nt' and self.path.stat().st_mode&0o077:
            raise ValueError('AUTH_CACHE_FILE_PERMISSIONS_REQUIRED')
        try:
            value=json.loads(self.path.read_text())
            if not isinstance(value,dict) or set(value)!={'version','client_hash','token','valid_until'} or value['version']!=1:
                raise ValueError()
            if not isinstance(value['token'],str) or not 1<=len(value['token'])<=8192 or isinstance(value['valid_until'],bool) or not isinstance(value['valid_until'],(int,float)) or not math.isfinite(value['valid_until']):
                raise ValueError()
        except (ValueError,TypeError,UnicodeError):raise ValueError('SHARED_AUTH_CACHE_INVALID') from None
        if value['client_hash']!=self.binding:raise ValueError('SHARED_AUTH_CACHE_CLIENT_CHANGED')
        return value

    def write(self,value):
        fd,name=tempfile.mkstemp(prefix='.token-',dir=self.directory)
        try:
            with os.fdopen(fd,'w') as stream:
                if os.name!='nt':os.fchmod(stream.fileno(),0o600)
                json.dump(value,stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    def get(self,issue):
        with self.locked():
            cached=self.read()
            if cached and cached['valid_until']>self.clock():return cached['token']
            payload=issue()
            if not isinstance(payload,dict):raise ValueError('INVALID_SHARED_AUTH_RESPONSE')
            token=payload.get('access_token')
            ttl=payload.get('expires_in')
            if not isinstance(token,str) or not token.strip() or isinstance(ttl,bool) or not isinstance(ttl,int) or ttl<=0:
                raise ValueError('INVALID_SHARED_AUTH_RESPONSE')
            value={'version':1,'client_hash':self.binding,'token':token.strip(),
                   'valid_until':self.clock()+ttl-min(60,ttl/10)}
            self.write(value)
            return value['token']

    def invalidate(self,rejected_token):
        with self.locked():
            cached=self.read()
            # A late 401 from an older request must not expire a newer token.
            if cached and cached['token']==rejected_token:
                cached['valid_until']=0
                self.write(cached)


class SharedTossMarketDataClient(TossMarketDataClient):
    auth_mode='SHARED_CLIENT_TOKEN_CACHE'
    def __init__(self,client_id,client_secret,*,cache_dir=None,**kwargs):
        super().__init__(client_id,client_secret,**kwargs)
        directory=cache_dir or os.getenv('STOCK_TOSS_AUTH_CACHE_DIR')
        if not directory:raise ValueError('SHARED_AUTH_CACHE_CONFIGURATION_REQUIRED')
        self.shared_tokens=SharedTokenCache(directory,client_id)
    def _token(self):
        self._access_token=self.shared_tokens.get(lambda:self._post_form(self._base_url+'/oauth2/token',
            form={'grant_type':'client_credentials','client_id':self._client_id,'client_secret':self._client_secret}).payload)
        return self._access_token
    def mark_rejected(self):
        self.shared_tokens.invalidate(self._access_token)
        self._access_token=None
    def prices(self,*args,**kwargs):
        from stock_assistant.providers.http import AuthenticationError
        try:return super().prices(*args,**kwargs)
        except AuthenticationError:self.mark_rejected();raise
    def candles(self,*args,**kwargs):
        from stock_assistant.providers.http import AuthenticationError
        try:return super().candles(*args,**kwargs)
        except AuthenticationError:self.mark_rejected();raise
