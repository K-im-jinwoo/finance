from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .ingestion import KrxHistoryIngestor, KrxDailyDataUnavailable
from .providers.http import ProviderError
from .repository import StockRepository


KST = timezone(timedelta(hours=9))
MARKETS = ("KOSPI", "KOSDAQ", "ETF")


class DailyRefresh:
    """Catch up published daily bars using the provider's verified market calendar."""

    def __init__(self, repository: StockRepository, ingestor: KrxHistoryIngestor,
                 calendar: Callable[[date], dict], *, clock=None):
        self.repository = repository
        self.ingestor = ingestor
        self.calendar = calendar
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, *, max_calendar_days: int = 31, include_completed_today: bool = False,
            allow_unpublished_latest: bool = False) -> dict:
        if isinstance(max_calendar_days, bool) or not 1 <= max_calendar_days <= 90:
            raise ValueError("max_calendar_days must be between 1 and 90")
        if not isinstance(include_completed_today, bool):
            raise ValueError("include_completed_today must be boolean")
        if not isinstance(allow_unpublished_latest, bool):
            raise ValueError("allow_unpublished_latest must be boolean")
        started = self.clock()
        if started.tzinfo is None:
            raise ValueError("refresh clock must be timezone-aware")
        today = started.astimezone(KST).date()
        self.repository.save_daily_check({'status': 'FAILED', 'checked_at': started.isoformat(),
                                          'target_date': None, 'error_class': 'REFRESH_IN_PROGRESS'})
        target = None
        saved = 0
        collected = []
        skipped = []
        try:
            calendar = self.calendar(today)
            target = date.fromisoformat(calendar['previousBusinessDay']['date'])
            if target >= today or not calendar['previousBusinessDay']['integrated'].get('regularMarket'):
                raise ValueError("calendar did not verify a previous trading day")
            if include_completed_today:
                session = calendar['today']
                if date.fromisoformat(session['date']) != today:
                    raise ValueError("calendar date does not match today's date")
                regular = session['integrated'].get('regularMarket')
                if regular:
                    if not isinstance(regular, dict) or not regular.get('endTime'):
                        raise ValueError("calendar did not verify today's market close")
                    closed = datetime.fromisoformat(regular['endTime'].replace('Z', '+00:00'))
                    if closed.tzinfo is None or closed.astimezone(KST).date() != today:
                        raise ValueError("market close date does not match calendar session")
                    if closed <= started:
                        target = today
            current = self.repository.daily_market_dates(as_of=started)
            if not all(market in current for market in MARKETS):
                raise ValueError("daily catch-up requires existing history for all three markets")
            first = min(date.fromisoformat(current[market]) for market in MARKETS) + timedelta(days=1)
            if (target - first).days + 1 > max_calendar_days:
                raise ValueError("daily catch-up exceeds bounded calendar window")
            day = first
            while day <= target:
                if day.weekday() >= 5:
                    skipped.append(day.isoformat())
                else:
                    session = self.calendar(day)['today']
                    if date.fromisoformat(session['date']) != day:
                        raise ValueError("calendar date does not match catch-up date")
                    if session['integrated'].get('regularMarket'):
                        summary = self.ingestor.ingest_day(
                            business_date=day, observed_at=self.clock(), require_complete=True,
                        )
                        saved += summary.bars_saved
                        collected.append(day.isoformat())
                    else:
                        skipped.append(day.isoformat())
                day += timedelta(days=1)
            completed = self.clock()
            dates = self.repository.daily_market_dates(as_of=completed)
            if any(dates.get(market) != target.isoformat() for market in MARKETS):
                raise ValueError("daily data is not complete through the verified trading date")
            result = {'status': 'READY', 'checked_at': completed.isoformat(),
                      'target_date': target.isoformat(), 'market_dates': dates,
                      'collected_dates': collected, 'skipped_dates': skipped, 'bars_saved': saved,
                      'calendar_source': 'TOSS_SECURITIES_OPEN_API', 'price_source': 'KRX_OPEN_API'}
            self.repository.save_daily_check(result)
            return result
        except (ValueError, KeyError, TypeError, OSError, ProviderError) as exc:
            if allow_unpublished_latest and isinstance(exc, KrxDailyDataUnavailable) and exc.business_date == target:
                completed = self.clock()
                pending = {'status': 'VERIFY_PENDING', 'checked_at': completed.isoformat(),
                           'target_date': target.isoformat(), 'error_code': exc.code, 'missing_market': exc.market,
                           'market_dates': self.repository.daily_market_dates(as_of=completed),
                           'collected_dates': collected, 'skipped_dates': skipped, 'bars_saved': saved,
                           'calendar_source': 'TOSS_SECURITIES_OPEN_API', 'price_source': 'KRX_OPEN_API'}
                # The legacy price-readiness contract stays FAILED; only research can continue.
                self.repository.save_daily_check({**pending,'status':'FAILED','research_status':'VERIFY_PENDING'})
                return pending
            failure = {
                'status': 'FAILED', 'checked_at': self.clock().isoformat(),
                'target_date': target.isoformat() if target else None,
                'error_class': type(exc).__name__, 'collected_dates': collected,
            }
            if getattr(exc, 'request_context', None):
                failure['request'] = exc.request_context
            self.repository.save_daily_check(failure)
            raise
