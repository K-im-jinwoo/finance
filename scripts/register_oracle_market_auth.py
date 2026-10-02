"""Run via sudo on Oracle. Credentials enter stdin only; never echo or log them."""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT=Path('/srv/stock-dashboard')
OPERATING_ID=Path('/srv/stock-assistant/toss-client-id')
def register(payload):
    if not isinstance(payload,dict) or set(payload) not in ({'client_id','client_secret'},{'access_token'}):raise ValueError('INVALID_FIELDS')
    values={k:v.strip() if isinstance(v,str) else '' for k,v in payload.items()}
    token_mode='access_token' in values
    if token_mode:
        token=values['access_token']
        if token.startswith('Bearer '):token=token[7:].strip()
        if token.lower()=='bearer' or not 1<=len(token)<=8192 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token):raise ValueError('INVALID_AUTH_FORMAT')
        values['access_token']=token
    elif not all(1<=len(v)<=512 and not any(ch.isspace() for ch in v) for v in values.values()):
        raise ValueError('INVALID_AUTH_FORMAT')
    if not token_mode and values['client_id']==OPERATING_ID.read_text().strip():
        raise ValueError('SHARED_OPERATING_CLIENT_FORBIDDEN')
    secrets=ROOT/'secrets'
    if ROOT.is_symlink() or ROOT.resolve()!=ROOT or secrets.is_symlink() or secrets.resolve()!=ROOT/'secrets':
        raise ValueError('UNEXPECTED_SECRET_TARGET')
    other_names=('dashboard-toss-client-id','dashboard-toss-client-secret') if token_mode else ('dashboard-toss-access-token',)
    for name in other_names:
        path=secrets/name
        if path.is_symlink():raise ValueError('UNEXPECTED_SECRET_TARGET')
        if path.is_file() and path.read_text().strip():raise ValueError('AUTH_MODE_CONFLICT')
    targets={k:secrets/('dashboard-toss-'+k.replace('_','-')) for k in values}
    for key,path in targets.items():
        if path.is_symlink():raise ValueError('UNEXPECTED_SECRET_TARGET')
        if not token_mode and path.exists() and path.read_text().strip() not in {'',values[key]}:
            raise ValueError('EXISTING_SEPARATE_AUTH_CONFLICT')
    # The directory is bound read-only into the separate service: atomic replacements
    # become visible without remounting the operating stock container.
    for key,path in targets.items():
        fd,name=tempfile.mkstemp(prefix='.auth-',dir=secrets)
        try:
            with os.fdopen(fd,'w') as stream:
                os.fchmod(stream.fileno(),0o600)
                os.fchown(stream.fileno(),1001,1001)
                stream.write(values[key]+'\n')
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
    return {'status':'REGISTERED_SUPPLIED_ACCESS_TOKEN' if token_mode else 'REGISTERED_SEPARATE_PUBLIC_AUTH',
            'credential_values_returned':False,'operating_auth_changed':False,'token_reissued':False}

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(16385)
        if len(raw)>16384:raise ValueError('INPUT_TOO_LARGE')
        result=register(json.loads(raw))
    except Exception as exc:
        allowed={'INVALID_FIELDS','INVALID_AUTH_FORMAT','SHARED_OPERATING_CLIENT_FORBIDDEN','UNEXPECTED_SECRET_TARGET','EXISTING_SEPARATE_AUTH_CONFLICT','AUTH_MODE_CONFLICT','INPUT_TOO_LARGE'}
        code=str(exc) if type(exc) is ValueError and str(exc) in allowed else type(exc).__name__
        result={'error':code,'credential_values_returned':False}
    print(json.dumps(result))
    sys.exit(1 if 'error' in result else 0)
