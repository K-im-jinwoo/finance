"""Korean exchange inputs and deterministic safety gates for swing screening."""
from datetime import timedelta
from decimal import Decimal
from html import unescape
from itertools import batched
import re

from .identifiers import require_korean_security_symbol
from .models import AssetType
from .news_research import event_from_dict, score_news
from .providers.http import UpstreamSchemaError, get_bytes

MARKETS = ('KOSPI', 'KOSDAQ')
MIN_MARKET_CAP = Decimal('50000000000')
KIND_MANAGEMENT_URL = 'https://kind.krx.co.kr/investwarn/adminissue.do'
BLOCKED_WARNINGS = {'INVESTMENT_WARNING', 'INVESTMENT_RISK', 'LIQUIDATION_TRADING'}


def managed_equity_codes(*, fetch_bytes=get_bytes):
    response = fetch_bytes(KIND_MANAGEMENT_URL, query={
        'method': 'searchAdminIssueSub', 'forward': 'adminissue_down',
        'currentPageSize': '3000', 'pageIndex': '1', 'orderMode': '1', 'orderStat': 'D',
    }, timeout_seconds=10)
    try:
        raw = response.payload
        if len(raw) > 2_000_000:
            raise ValueError
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = raw.decode('cp949')
        table = re.search(r'<table\b[^>]*class=[\'\"]bbs_tb[\'\"][^>]*>(.*?)</table>', text, re.I | re.S)
        if table is None or not re.search(r'<th\b[^>]*>\s*종목코드\s*</th>', table[1]):
            raise ValueError
        codes = set()
        rows = re.findall(r'<tr\b[^>]*>(.*?)</tr>', table[1], re.I | re.S)
        data_rows = [row for row in rows if '<td' in row.lower()]
        if len(data_rows) >= 3000 or not data_rows:
            raise ValueError
        for row in data_rows:
            cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.I | re.S)
            if len(cells) == 1 and re.search(r'조회된.*없습니다', cells[0]):
                continue
            if len(cells) < 3:
                raise ValueError
            code = unescape(re.sub(r'<[^>]+>', '', cells[1])).strip()
            codes.add(require_korean_security_symbol(code))
        return codes
    except (ValueError, UnicodeError):
        raise UpstreamSchemaError('KIND_MANAGEMENT_LIST_INCOMPLETE') from None


def swing_price_signal(bars, turnover):
    if len(bars) != 21 or len(turnover) != 21:
        return None
    latest = bars[-1]
    average = sum(turnover[:-1]) / 20
    sma20 = sum(bar.close for bar in bars[-20:]) / 20
    if average <= 0 or latest.volume <= 0 or latest.close <= sma20 or bars[-6].close <= 0:
        return None
    multiple = turnover[-1] / average
    gain5 = latest.close / bars[-6].close - 1
    if turnover[-1] < 3_000_000_000 or multiple < Decimal('2.5') or gain5 > Decimal('0.25'):
        return None
    return {'turnover': turnover[-1], 'turnover_multiple': multiple, 'sma20': sma20,
            'close': latest.close, 'return_5d': gain5}


def _krx_values(store, client, market, sessions, *, clock, sleeper):
    values = []
    for session in sessions:
        key = f'{market}:{session.isoformat()}'
        cached = store.latest('swing_krx_values', key, as_of=clock())
        if cached:
            values.append(cached['payload'])
            continue
        rows = client.equity_values(market, session)
        sleeper(0.1)
        if not rows:
            raise UpstreamSchemaError('SWING_KRX_EQUITY_VALUES_UNAVAILABLE')
        store.put('swing_krx_values', key, rows, observed_at=clock())
        values.append(rows)
    return values


def rank_swing_prices(repository, store, krx_client, *, sessions, clock, sleeper):
    as_of = clock()
    securities = [row for row in repository.list_securities()
                  if row.asset_type is AssetType.COMMON and row.market.value in MARKETS
                  and (row.listed_on is None or row.listed_on <= sessions[-1])
                  and (row.delisted_on is None or row.delisted_on > sessions[-1])]
    prices = []
    for market in MARKETS:
        market_securities = [row for row in securities if row.market.value == market]
        if not market_securities:
            continue
        values = _krx_values(store, krx_client, market, sessions, clock=clock, sleeper=sleeper)
        for security in market_securities:
            symbol = security.symbol
            if any(symbol not in day for day in values):
                continue
            cap = Decimal(values[-1][symbol]['market_cap'])
            if cap < MIN_MARKET_CAP or Decimal(values[-1][symbol]['turnover']) < 3_000_000_000:
                continue
            bars = [bar for bar in repository.bars_for(symbol, as_of=as_of, limit=25)
                    if bar.source == 'KRX_OPEN_API' and bar.trade_date <= sessions[-1]][-21:]
            if tuple(bar.trade_date for bar in bars) != sessions:
                continue
            signal = swing_price_signal(bars, [Decimal(day[symbol]['turnover']) for day in values])
            if signal:
                prices.append((security, {**signal, 'market_cap': cap}))
    return sorted(prices, key=lambda row: (-row[1]['turnover_multiple'], -row[1]['turnover'], row[0].symbol))


def safe_swing_shortlist(store, market_client, ranked, *, managed, clock, sleeper, limit):
    results = []
    as_of = clock()
    eligible = []
    for security, signal in ranked:
        symbol = security.symbol
        if symbol in managed:
            continue
        events = [event_from_dict(record['payload'])
                  for record in store.records('reviewed_event', symbol, as_of=as_of)]
        if score_news(symbol, events, None, as_of=as_of)['hard_exclusions']:
            continue
        eligible.append((security, signal))
    for batch in batched(eligible, 200):
        reference = market_client.stock_reference([security.symbol for security, _ in batch])
        sleeper(0.25)
        for security, signal in batch:
            row = reference.get(security.symbol, {})
            detail = row.get('koreanMarketDetail') or {}
            if (row.get('market') != security.market.value or row.get('status') != 'ACTIVE'
                    or not isinstance(detail, dict) or detail.get('krxTradingSuspended') is not False
                    or detail.get('liquidationTrading') is not False):
                continue
            warnings = market_client.stock_warnings(security.symbol)
            sleeper(0.25)
            if warnings & BLOCKED_WARNINGS:
                continue
            results.append((security, signal))
            if len(results) == limit:
                return results
    return results
