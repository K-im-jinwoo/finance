"""Explicit live transports. Secrets remain in memory and never enter diagnostics."""
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from .http import HttpResult


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('REDIRECT_REFUSED')


def read_token():
    # SharedTokenCache.read is the only cache operation used: no lock-file
    # creation, OAuth issuance, invalidation or mutation is permitted.
    from shared_toss_auth import SharedTokenCache
    client_path = Path(os.environ['DAYTRADE_CLIENT_ID_FILE'])
    cache = SharedTokenCache(os.environ['DAYTRADE_TOKEN_CACHE'], client_path.read_text().strip())
    if cache.path.is_symlink():
        raise ValueError('AUTH_CACHE_SYMLINK_REFUSED')
    cached = cache.read()
    if not cached or cached['valid_until'] - time.time() < 120:
        raise RuntimeError('SHARED_TOKEN_EXPIRING_NO_RENEWAL')
    return cached['token']


class ReadOnlyTransport:
    allowed = frozenset({'/api/v1/market-calendar/KR', '/api/v1/rankings', '/api/v1/stocks'})

    def __init__(self, token, stop, *, opener=None, clock=time.monotonic):
        self.token, self.stop, self.clock = token, stop, clock
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.last_end = float('-inf')
        self.statuses, self.requests = {}, []

    def __call__(self, path, query):
        if path not in self.allowed:
            raise ValueError('PUBLIC_READ_SCOPE_REQUIRED')
        if self.stop.wait(max(0, 2.5 - (self.clock() - self.last_end))):
            raise RuntimeError('STOPPING')
        started = self.clock()
        request = urllib.request.Request('https://openapi.tossinvest.com' + path + '?' + urllib.parse.urlencode(query),
            headers={'Authorization': 'Bearer ' + self.token(), 'Accept': 'application/json'}, method='GET')
        try:
            try:
                response = self.opener.open(request, timeout=8)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status
                self.statuses[str(status)] = self.statuses.get(str(status), 0) + 1
                self.requests.append({'path': path, 'started_monotonic': started, 'http_status': status})
                # Bounded diagnostics for an all-day process; counts remain total.
                self.requests = self.requests[-1000:]
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('BOUNDED_RESPONSE_REQUIRED')
                value = json.loads(raw) if status == 200 else {}
                return HttpResult(status, value, {k:v for k,v in response.headers.items() if k.lower() == 'retry-after'})
        finally:
            self.last_end = self.clock()


class TelegramSender:
    def __init__(self, token, recipient, *, opener=None):
        self.token, self.recipient = token, recipient
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __call__(self, text):
        status = None
        try:
            body = json.dumps({'chat_id': self.recipient, 'text': text, 'disable_notification': False}).encode()
            request = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/sendMessage',
                data=body, headers={'Content-Type': 'application/json'}, method='POST')
            with self.opener.open(request, timeout=1.5) as response:
                status = response.status
                raw = response.read(1_000_001)
                if status != 200 or len(raw) > 1_000_000:
                    return {'status': 'FAILED', 'http_status': status}
                value = json.loads(raw)
                result = value.get('result', {})
                if value.get('ok') is True and type(result.get('message_id')) is int and str(result.get('chat', {}).get('id')) == self.recipient:
                    return {'status': 'SENT', 'http_status': 200}
        except Exception:
            pass
        return {'status': 'FAILED', 'http_status': status}


def telegram_sender():
    from dotenv import dotenv_values
    profile = Path(os.environ['DAYTRADE_TELEGRAM_PROFILE'])
    env = dotenv_values(profile/'.env')
    token, recipient = env.get('TELEGRAM_BOT_TOKEN'), env.get('TELEGRAM_HOME_CHANNEL')
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]{5,}:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('EXISTING_BOT_TOKEN_REQUIRED')
    if not isinstance(recipient, str) or not re.fullmatch(r'-?[0-9]+', recipient):
        raise ValueError('EXISTING_HOME_CHANNEL_REQUIRED')
    with sqlite3.connect((profile/'state.db').as_uri() + '?mode=ro', uri=True) as connection:
        rows = connection.execute("SELECT chat_id,thread_id FROM sessions WHERE source='telegram'").fetchall()
    if len(rows) != 1 or str(rows[0][0]) != recipient or rows[0][1] is not None:
        raise ValueError('APPROVED_TARGET_AMBIGUOUS')
    return TelegramSender(token, recipient)
