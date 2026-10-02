"""Approved Naver registration over SSH stdin; no credential values in output."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from datetime import datetime, timezone


ROOT = Path('/srv/stock-assistant')


def reserve_news_auth_call() -> None:
    # Share the runtime's durable counter; missing installation/ledger fails closed.
    source = ROOT / 'current/src'
    if not (source / 'stock_assistant/news_cost.py').is_file():
        raise ValueError('NAVER_COST_GUARD_UNAVAILABLE')
    sys.path.insert(0, str(source))
    try:
        from stock_assistant.news_cost import NewsApiBudget
        NewsApiBudget(ROOT / 'state/news-api-usage.sqlite3').reserve(
            1, at=datetime.now(timezone.utc), cooldown=False)
    except Exception as exc:
        code = getattr(exc, 'code', 'NAVER_COST_GUARD_UNAVAILABLE')
        raise ValueError(code) from None
    finally:
        sys.path.pop(0)


def verify_credentials(values: dict) -> None:
    reserve_news_auth_call()
    query = urllib.parse.urlencode({'query': '특징주', 'display': 1, 'sort': 'date', 'format': 'json'})
    request = urllib.request.Request('https://naverapihub.apigw.ntruss.com/search/v1/news?' + query,
                                    headers={'X-NCP-APIGW-API-KEY-ID': values['client_id'],
                                             'X-NCP-APIGW-API-KEY': values['client_secret']})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError('NAVER_RESPONSE_INVALID')
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
            raise ValueError('NAVER_RESPONSE_INVALID')
    except urllib.error.HTTPError as exc:
        code = {401: 'NAVER_AUTH_REJECTED', 403: 'NAVER_SEARCH_PERMISSION_REQUIRED',
                429: 'NAVER_RATE_LIMITED'}.get(exc.code, 'NAVER_CHECK_UNAVAILABLE')
        raise ValueError(code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('NAVER_CHECK_UNAVAILABLE') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('NAVER_RESPONSE_INVALID') from None


def register(payload: dict, *, root: Path = ROOT, owner: tuple[int, int] = (1001, 1001),
             verifier: Callable = verify_credentials) -> dict:
    if not isinstance(payload, dict) or set(payload) != {'client_id', 'client_secret'}:
        raise ValueError('INVALID_FIELDS')
    values = {k: v.strip() if isinstance(v, str) else '' for k, v in payload.items()}
    if not all(re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,512}', v) for v in values.values()):
        raise ValueError('INVALID_AUTH_FORMAT')
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError('UNEXPECTED_SECRET_TARGET')
    schedule = root / 'schedule.env'
    if schedule.is_file() and re.search(r'^\s*(?:export\s+)?STOCK_NEWS_ENABLED\s*=\s*[\"\']?true(?:[\"\']?\s*)$', schedule.read_text(), re.M):
        raise ValueError('ACTIVE_NEWS_AUTH_REQUIRES_CUTOVER')
    targets = {k: root / ('naver-news-' + k.replace('_', '-')) for k in values}
    previous = {}
    for key, target in targets.items():
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError('UNEXPECTED_SECRET_TARGET')
        previous[key] = target.read_text().strip() if target.is_file() else ''
    # Reject invalid input before creating or replacing any credential files.
    verifier(values)
    replaced_invalid = False
    if any(previous[k] not in {'', values[k]} for k in values):
        if not all(previous.values()):
            raise ValueError('EXISTING_AUTH_CONFLICT')
        try:
            verifier(previous)
        except ValueError as exc:
            if str(exc) not in {'NAVER_AUTH_REJECTED', 'NAVER_SEARCH_PERMISSION_REQUIRED'}:
                raise
            replaced_invalid = True
        else:
            raise ValueError('EXISTING_AUTH_CONFLICT')
    pending = []
    try:
        # Prepare both values before replacing either target. Never log or return them.
        for key, target in targets.items():
            fd, name = tempfile.mkstemp(prefix='.naver-auth-', dir=root)
            pending.append((Path(name), target))
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                os.fchown(stream.fileno(), *owner)
                stream.write(values[key] + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, target in pending:
            os.replace(temporary, target)
    finally:
        for temporary, _ in pending:
            if temporary.exists():
                temporary.unlink()
    return {'status': 'REGISTERED_NAVER_NEWS_AUTH', 'credential_values_returned': False,
            'news_auth_verified': True, 'replaced_invalid_auth': replaced_invalid,
            'news_enabled': False, 'messages_sent': 0}


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError('INPUT_TOO_LARGE')
        result = register(json.loads(raw))
    except Exception as exc:
        safe = {'INVALID_FIELDS', 'INVALID_AUTH_FORMAT', 'UNEXPECTED_SECRET_TARGET',
                'EXISTING_AUTH_CONFLICT', 'INPUT_TOO_LARGE', 'NAVER_AUTH_REJECTED',
                'NAVER_SEARCH_PERMISSION_REQUIRED', 'NAVER_RATE_LIMITED', 'NAVER_CHECK_UNAVAILABLE',
                'NAVER_RESPONSE_INVALID', 'ACTIVE_NEWS_AUTH_REQUIRES_CUTOVER',
                'NAVER_COST_GUARD_UNAVAILABLE', 'NEWS_FREE_POLICY_UNCONFIRMED',
                'NEWS_COST_LIMIT_REACHED', 'NEWS_USAGE_STATE_UNAVAILABLE',
                'NEWS_PROVIDER_QUOTA_BLOCKED', 'NEWS_USAGE_CLOCK_REVERSED'}
        code = str(exc) if type(exc) is ValueError and str(exc) in safe else type(exc).__name__
        result = {'error': code, 'credential_values_returned': False}
    print(json.dumps(result))
    return 1 if 'error' in result else 0


if __name__ == '__main__':
    raise SystemExit(main())
