"""Daily swing recommendation snapshot. Screening and evidence are code-owned."""
from datetime import datetime, timedelta, timezone
import hashlib
import re
from time import sleep

from .backtest import SWING_DECISION, SWING_POLICY
from .models import to_json_value
from .news_discovery import _matches, discover_news
from .providers.news import NewsArticle
from .research_workflow import collect_calendar, market_session
from .swing_market import managed_equity_codes, rank_swing_prices, safe_swing_shortlist

SWING_NEWS_LIMIT = 15
KST = timezone(timedelta(hours=9))
THEME_EVENTS = ('신제품출시', '서비스출시', '상용화완료', '양산개시', '공장가동', '수출개시', '공급개시')
SPECULATION = ('기대', '전망', '예정', '논의', '루머', '검토', '가능성', '유력', '수주전',
               '입찰참여', '부인', '취소', '미확정', '상용화추진', '해지', '오보', '사실무근')
MARKET_CHATTER = ('관련주', '수혜주', '테마주', '부각')
POLICY_ACTIONS = ('확정', '선정', '시행', '통과', '공고', '승인')


def swing_report_id(session):
    digest = hashlib.sha256(f'{SWING_POLICY}:{session.isoformat()}'.encode()).hexdigest()
    return 'SW-' + digest[:24]


def swing_catalysts(security, articles, *, as_of):
    evidence = {}
    risk = False
    items = sorted(articles, key=lambda article: (-article.published_at.timestamp(), article.url))
    for article in items:
        if not (as_of - timedelta(hours=72) <= article.published_at <= article.observed_at <= as_of):
            continue
        title = article.title.casefold()
        if not _matches(article.title, security.name, title):
            continue
        compact = re.sub(r'\s+', '', title)
        events = discover_news([article], [security], as_of=as_of, lookback_hours=72)['events']
        category = events[0]['category'] if events else None
        if category == 'NEWS_RISK':
            risk = True
            continue
        if any(term in compact for term in (*SPECULATION, *MARKET_CHATTER)):
            continue
        if category == 'POLICY' and not any(term in compact for term in POLICY_ACTIONS):
            continue
        if category is None and any(term in compact for term in THEME_EVENTS):
            category = 'THEME'
        if category is None:
            continue
        title_key = re.sub(r'[^0-9a-z가-힣]', '', title)
        if title_key not in evidence and all(row['url'] != article.url for row in evidence.values()):
            evidence[title_key] = {'category': category, 'title': article.title, 'url': article.url,
                                   'excerpt': article.description[:220],
                                   'published_at': article.published_at.isoformat()}
    return [] if risk else list(evidence.values())[:3]


def _swing_news(store, client, shortlist, *, session, clock):
    results = {}
    missing = []
    for security, _ in shortlist:
        record = store.latest('swing_news_snapshot', f'{session}:{security.symbol}', as_of=clock())
        if record:
            results[security.symbol] = record['payload']['articles']
        else:
            missing.append(security)
    if missing:
        client.archive_batch(len(missing))
        for security in missing:
            page = client.archive_page(query=security.name, start=1)
            articles = [article for article in page.articles if _matches(article.title, security.name, article.title.casefold())]
            encoded = to_json_value(articles)
            store.put('swing_news_snapshot', f'{session}:{security.symbol}', {'articles': encoded}, observed_at=page.observed_at)
            results[security.symbol] = encoded
    return {symbol: [NewsArticle(title=row['title'], url=row['url'],
                                 published_at=datetime.fromisoformat(row['published_at']),
                                 observed_at=datetime.fromisoformat(row['observed_at']),
                                 description=row['description']) for row in articles]
            for symbol, articles in results.items()}


def refresh_swing(repository, store, *, krx_client, market_client, news_client,
                  management_list=managed_equity_codes, clock=None, sleeper=sleep):
    clock = clock or (lambda: datetime.now(timezone.utc))
    collect_calendar(store, market_client, clock=clock)
    now = clock()
    pending = {'contract': SWING_POLICY, 'status': 'VERIFY_PENDING', 'orders_enabled': False, 'candidates': []}
    calendar = store.latest('market_calendar', 'KR', as_of=now)
    if calendar and calendar['payload']['date'] == now.astimezone(KST).date().isoformat():
        today = calendar['payload']['calendar']['today']
        if not today['integrated'].get('regularMarket'):
            return {**pending, 'status': 'SKIPPED', 'reason': 'SWING_NON_TRADING_DAY'}
    session, _ = market_session(store, as_of=now)
    check = repository.latest_daily_check(as_of=now)
    if session is None or not check or check.get('status') != 'READY' or check.get('target_date') != session.isoformat():
        return {**pending, 'reason': 'SWING_KRX_DAILY_UNVERIFIED'}
    previous = store.latest('swing_report', session.isoformat(), as_of=now)
    if previous:
        return previous['payload']
    sessions = tuple(day for day in repository.krx_sessions(after=session - timedelta(days=60), as_of=now)
                     if day <= session)[-21:]
    if len(sessions) != 21 or sessions[-1] != session:
        return {**pending, 'reason': 'SWING_KRX_HISTORY_INCOMPLETE'}

    ranked = rank_swing_prices(repository, store, krx_client, sessions=sessions, clock=clock, sleeper=sleeper)
    shortlist = safe_swing_shortlist(store, market_client, ranked, managed=management_list(),
                                     clock=clock, sleeper=sleeper, limit=SWING_NEWS_LIMIT) if ranked else []
    news = _swing_news(store, news_client, shortlist, session=session, clock=clock)
    now = clock()
    candidates = []
    for security, signal in shortlist:
        evidence = swing_catalysts(security, news[security.symbol], as_of=now)
        if evidence:
            candidates.append({'symbol': security.symbol, 'name': security.name, **signal,
                               'news_reference_date': datetime.fromisoformat(evidence[0]['published_at']).astimezone(KST).date(),
                               'news_evidence': evidence, 'action_decision': SWING_DECISION,
                               'expected_holding_period': '3/5/10 거래일', 'price': {'strategy': SWING_POLICY}})
        if len(candidates) == 3:
            break
    report = to_json_value({'contract': SWING_POLICY, 'policy_version': SWING_POLICY, 'report_id': swing_report_id(session),
                           'as_of': now, 'price_session': session, 'status': 'READY', 'orders_enabled': False,
                           'news_checked_count': len(shortlist), 'candidates': candidates})
    return store.publish_swing_report(session, report, observed_at=now)
