"""Explicit live transports. Secrets remain in memory and never enter diagnostics."""
from contextlib import closing, suppress
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from .http import HttpResult
from ..telegram_routing import BotChannel, resolve_telegram_route, telegram_bot_id, with_channel_header


TELEGRAM_TIMEOUT_SECONDS = 5.0


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
    def __init__(self, token, recipient=None, *, bot_channel=BotChannel.DAYTRADE, env=None, opener=None,
                 timeout=TELEGRAM_TIMEOUT_SECONDS):
        if type(timeout) not in (int, float) or not 0 < timeout < float('inf'):
            raise ValueError('TELEGRAM_TIMEOUT_MUST_BE_POSITIVE_AND_FINITE')
        self.timeout = timeout
        self.token = token
        self.route = resolve_telegram_route(bot_channel, os.environ if env is None else env, default_chat_id=recipient)
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def __call__(self, text):
        status = None
        try:
            payload = {'chat_id': self.route.chat_id, 'text': with_channel_header(text, self.route.bot_channel),
                       'disable_notification': False}
            if self.route.message_thread_id is not None:
                payload['message_thread_id'] = self.route.message_thread_id
            body = json.dumps(payload).encode()
            request = urllib.request.Request('https://api.telegram.org/bot' + self.token + '/sendMessage',
                data=body, headers={'Content-Type': 'application/json'}, method='POST')
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(1_000_001)
                if status != 200 or len(raw) > 1_000_000:
                    return {'status': 'FAILED', 'http_status': status}
                value = json.loads(raw)
                result = value.get('result', {})
                if value.get('ok') is True and type(result.get('message_id')) is int and str(result.get('chat', {}).get('id')) == self.route.chat_id:
                    return {'status': 'SENT', 'http_status': 200}
        except urllib.error.HTTPError as error:
            code = error.code
            status = code if isinstance(code, int) and 100 <= code <= 599 else None
            with suppress(Exception):
                error.close()
        except Exception:
            pass
        return {'status': 'FAILED', 'http_status': status}


def telegram_sender():
    from dotenv import dotenv_values
    profile = Path(os.environ['DAYTRADE_TELEGRAM_PROFILE'])
    env = dotenv_values(profile/'.env')
    route_env = {**env, **os.environ, 'TELEGRAM_HOME_CHANNEL': env.get('TELEGRAM_HOME_CHANNEL')}
    dedicated_token = (route_env.get('TELEGRAM_DAYTRADE_BOT_TOKEN') or '').strip()
    token = dedicated_token or (env.get('TELEGRAM_BOT_TOKEN') or '').strip()
    if telegram_bot_id(token) is None:
        raise ValueError('EXISTING_BOT_TOKEN_REQUIRED')
    sender = TelegramSender(token, bot_channel=BotChannel.DAYTRADE, env=route_env)
    if not sender.route.dedicated_chat:
        with closing(sqlite3.connect((profile/'state.db').as_uri() + '?mode=ro', uri=True)) as connection:
            row = connection.execute("SELECT 1 FROM sessions WHERE source='telegram' AND CAST(chat_id AS TEXT)=? "
                                     "AND thread_id IS NULL LIMIT 1", (sender.route.chat_id,)).fetchone()
        if row is None:
            raise ValueError('APPROVED_TARGET_AMBIGUOUS')
    return sender
