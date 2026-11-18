"""Approved split windows; these are research coverage, never trading authority."""
from calendar import monthrange
from datetime import date, timedelta, timezone

KST = timezone(timedelta(hours=9))
RECENT_NEWS_DAYS = 90
RISK_HISTORY_YEARS = 3
OFFICIAL_HISTORY_YEARS = 10
RISK_QUERY_VERSION = 'material-risk-3y-v2'
RISK_QUERY_PLAN = (('횡령', 'MANAGEMENT'), ('배임', 'MANAGEMENT'), ('감사의견', 'MANAGEMENT'),
                   ('유상증자', 'DILUTION'), ('전환사채', 'DILUTION'), ('신주인수권부사채', 'DILUTION'),
                   ('계약해지', 'BUSINESS_DAMAGE'), ('거래정지', 'BUSINESS_DAMAGE'),
                   ('상장폐지', 'BUSINESS_DAMAGE'), ('회생절차', 'BUSINESS_DAMAGE'))


def years_before(day, years):
    return date(day.year - years, day.month, min(day.day, monthrange(day.year - years, day.month)[1]))


def news_windows(as_of):
    end = as_of.astimezone(KST).date() - timedelta(days=1)
    return {'end': end, 'recent_start': end - timedelta(days=RECENT_NEWS_DAYS - 1),
            'risk_start': years_before(end, RISK_HISTORY_YEARS),
            'official_start': years_before(end, OFFICIAL_HISTORY_YEARS)}
