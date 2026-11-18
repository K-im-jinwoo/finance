"""Approved operational refresh; external reads, isolated research writes, no orders."""
from datetime import datetime, timezone
from pathlib import Path
import sys

from .dart_research import DartResearchCollector
from .news_archive import collect_archive
from .material_archive import collect_material_archive
from .official_events import review_official_contracts
from .providers.dart import DartClient
from .providers.http import ProviderError
from .research_workflow import collect_calendar, market_session
from .unified_research import build_recommendations
from .risk_history import refresh_risk_coverage
from .research_focus import choose_focus


class _ArchiveBatch:
    """Share one durable reservation between broad news and material searches."""
    def __init__(self, client, calls):
        if type(calls) is not int or not 1 <= calls <= 5:
            raise ValueError('archive batch must reserve one to five calls')
        self.client = client
        self.calls = calls
        self.reserved_calls = 0
        self.actual_calls = 0
        self.stage_remaining = 0
        self.failure = None

    def archive_batch(self, calls):
        if self.failure is not None:
            raise self.failure
        if type(calls) is not int or not 1 <= calls <= self.calls - self.actual_calls:
            raise ValueError('archive stage exceeds shared call budget')
        if not self.reserved_calls:
            try:
                self.client.archive_batch(self.calls)
            except ProviderError as exc:
                self.failure = exc
                raise
            self.reserved_calls = self.calls
        self.stage_remaining = calls

    def archive_page(self, **kwargs):
        if self.failure is not None:
            raise self.failure
        if self.stage_remaining < 1 or self.actual_calls >= self.reserved_calls:
            raise ValueError('archive page exceeds shared call budget')
        self.stage_remaining -= 1
        self.actual_calls += 1
        try:
            return self.client.archive_page(**kwargs)
        except ProviderError as exc:
            self.failure = exc
            raise


def refresh_unified(repository, store, *, dart_client, news_client, calendar_client, dart_key,
                    batch_size=25, archive_pages=5, clock=None, seed_symbols=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    batch = _ArchiveBatch(news_client, archive_pages)
    def focus_symbols():
        selected = choose_focus(repository, store, as_of=clock())
        store.put('research_focus', 'ALL', {'symbols': selected, 'limit': 10}, observed_at=clock())
        return selected
    focus_symbols()
    stages = {}
    try:
        stages['financials'] = DartResearchCollector(repository, store, dart_client, clock=clock).run(
            symbols=seed_symbols, batch_size=batch_size)
    except (ProviderError, ValueError) as exc:
        stages['financials'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
    focus = focus_symbols()
    stages['research_focus'] = {'symbols': focus, 'limit': 10}
    # Historical risk checks are required for every focused issuer, independently
    # of whether its recent news query reaches a provider limit.
    material_pages = min(2, archive_pages - 1) if focus else 0
    recent_pages = archive_pages - material_pages
    if archive_pages == 1 and focus:
        from .news_policy import news_windows
        end = news_windows(clock())['end'].isoformat()
        covered = store.latest_by_symbol('recent_news_coverage', as_of=clock())
        if all(s in covered and covered[s]['payload'].get('end', '') >= end for s in focus):
            recent_pages = 0
    try:
        stages['archive'] = (collect_archive(repository, store, batch, symbols=focus,
                                             pages=recent_pages, clock=clock, recent=True)
                             if recent_pages else {'status': 'CURRENT', 'requested': 0, 'actual_calls': 0})
    except (ProviderError, ValueError) as exc:
        stages['archive'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
    material_pages = archive_pages - batch.actual_calls if focus else 0
    if material_pages:
        try:
            stages['material_archive'] = collect_material_archive(repository, store, batch, symbols=focus,
                                                                  pages=material_pages, clock=clock, split_policy=True)
        except (ProviderError, ValueError) as exc:
            stages['material_archive'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
    stages['news_budget'] = {'reserved_calls': batch.reserved_calls, 'actual_calls': batch.actual_calls}
    stages['risk_coverage'] = {'completed': refresh_risk_coverage(store, focus, as_of=clock())}
    try:
        stages['official_events'] = review_official_contracts(store, api_key=dart_key, as_of=clock(), clock=clock)
    except (ProviderError, ValueError) as exc:
        stages['official_events'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
    try:
        stages['calendar'] = collect_calendar(store, calendar_client, clock=clock)
    except (ProviderError, ValueError) as exc:
        stages['calendar'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
    now = clock()
    session, closed = market_session(store, as_of=now)
    check = repository.latest_daily_check(as_of=now)
    from .unified_research import KST
    ready = bool(check and check.get('status')=='READY' and session and check.get('target_date')==session.isoformat()
                 and datetime.fromisoformat(check['checked_at']).astimezone(KST).date()==now.astimezone(KST).date())
    stages['prices']={k:check.get(k) for k in ['status','target_date','market_dates','error_code','missing_market']} if check else {'status':'UNAVAILABLE'}
    if check and check.get('research_status'):
        stages['prices']['status']=check['research_status']
    if not ready:
        stages['prices']['status'] = 'VERIFY_PENDING'
    stages['prices']['required_session'] = session.isoformat() if session else None
    report = build_recommendations(repository, store, as_of=now, expected_session=session, verified_close_at=closed,
                                   price_refresh_ready=ready)
    report['refresh_stages'] = stages
    # Actual refresh metadata belongs in the immutable report identity too.
    import hashlib, json
    report.pop('report_id', None)
    report['report_id'] = 'UR-' + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
    store.put('unified_report', 'ALL', report, observed_at=now)
    for row in report['evaluations']:
        if row['symbol'] in focus and row.get('recent_news_complete') and row.get('risk_history_complete'):
            store.put('search_round_completed', row['symbol'], {'status': 'SEARCH_COMPLETE',
                      'report_id': report['report_id'], 'end': report['research_windows']['end'],
                      'review_pending': bool(row['pending_checks'])}, observed_at=now)
    return report


def shared_calendar(client_id, client_secret):
    """Use the already installed common cache; never fall back to independent OAuth."""
    import os
    if not os.getenv('STOCK_TOSS_AUTH_CACHE_DIR'):
        raise ValueError('SHARED_AUTH_CACHE_CONFIGURATION_REQUIRED')
    if '/opt/stock-shared-auth' not in sys.path:
        sys.path.insert(0, '/opt/stock-shared-auth')
    from shared_toss_auth import SharedTossMarketDataClient
    return SharedTossMarketDataClient(client_id, client_secret)
