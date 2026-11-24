"""Approved operational refresh; external reads, isolated research writes, no orders."""
from datetime import date, datetime, timezone
from pathlib import Path
import sys

from .dart_research import DartResearchCollector
from .news_archive import collect_actionable_news, collect_archive
from .news_cost import MAX_UNIFIED_NEWS_CALLS
from .news_policy import news_windows
from .material_archive import collect_material_archive
from .official_events import review_official_contracts
from .providers.dart import DartClient
from .providers.http import ProviderError
from .research_workflow import collect_calendar, market_session
from .unified_research import build_recommendations
from .risk_history import refresh_risk_coverage
from .research_focus import choose_actionable_focus, choose_focus


class _ArchiveBatch:
    """Share one durable reservation between broad news and material searches."""
    def __init__(self, client, calls):
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


def _latest_available_price_session(repository, *, as_of: datetime) -> date | None:
    dates = repository.daily_market_dates(as_of=as_of)
    valid = [date.fromisoformat(value) for value in dates.values() if value]
    return min(valid) if valid else None


def _refresh_actionable_news(repository, store, batch, focus_symbols, *, build_report, clock):
    now = clock()
    end = news_windows(now)['end'].isoformat()
    fresh = set()
    for kind in ('recent_news_snapshot', 'recent_news_coverage'):
        fresh.update(symbol for symbol, record in store.latest_by_symbol(kind, as_of=now).items()
                     if record['payload'].get('end', '') >= end)
    report = build_report(now)
    results = []
    error = None
    expanded = False
    while batch.actual_calls < batch.calls:
        remaining = batch.calls - batch.actual_calls
        targets = [row['symbol'] for row in report['candidates'] if row['symbol'] not in fresh][:remaining]
        if not targets and not expanded:
            expanded = True
            # Keep room to refresh a replacement slate after the broader scan.
            room = max(0, remaining - 5) if batch.calls > 5 else remaining
            targets = [symbol for symbol in focus_symbols if symbol not in fresh][:room]
        if not targets:
            break
        try:
            stage = collect_actionable_news(repository, store, batch, symbols=targets, clock=clock, max_calls=len(targets))
            results.extend(stage['results'])
        except (ProviderError, ValueError) as exc:
            error = exc.code if isinstance(exc, ProviderError) else str(exc)
        fresh.update(targets)
        now = clock()
        report = build_report(now)
        if error:
            break

    status = 'CURRENT' if not results else (
        'COMPLETE' if all(row['status'] == 'COMPLETE' for row in results) else 'SNAPSHOT_100')
    archive = {'status': status, 'requested': len(results), 'actual_calls': batch.actual_calls, 'results': results}
    if error:
        archive.update(status='UNAVAILABLE', error_code=error)
    return report, archive, now


def refresh_unified(repository, store, *, dart_client, news_client, calendar_client, dart_key,
                    batch_size=25, archive_pages=None, clock=None, seed_symbols=None, actionable=False):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if not isinstance(actionable, bool):
        raise ValueError("actionable must be boolean")
    max_pages = MAX_UNIFIED_NEWS_CALLS if actionable else 5
    archive_pages = max_pages if archive_pages is None else archive_pages
    if type(archive_pages) is not int or not 1 <= archive_pages <= max_pages:
        raise ValueError('invalid unified news archive budget')
    batch = _ArchiveBatch(news_client, archive_pages)
    stages = {}

    if actionable:
        try:
            stages['calendar'] = collect_calendar(store, calendar_client, clock=clock)
        except (ProviderError, ValueError) as exc:
            stages['calendar'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
        now = clock()
        session, closed = market_session(store, as_of=now)
        check = repository.latest_daily_check(as_of=now)
        from .unified_research import KST
        ready = bool(check and check.get('status') == 'READY' and session and check.get('target_date') == session.isoformat()
                     and datetime.fromisoformat(check['checked_at']).astimezone(KST).date() == now.astimezone(KST).date())
        fallback_session = None if ready else _latest_available_price_session(repository, as_of=now)
        effective_session = session if ready else (fallback_session or session)
        effective_close = closed if ready else None

        pre_focus = choose_actionable_focus(
            repository, store, as_of=now,
            expected_session=effective_session, verified_close_at=effective_close,
            limit=min(5, archive_pages),
        )
        try:
            stages['financials'] = DartResearchCollector(repository, store, dart_client, clock=clock).run(
                symbols=seed_symbols, batch_size=batch_size,
                priority_symbols=pre_focus['ready_pool'] if seed_symbols is None else None,
            )
        except (ProviderError, ValueError) as exc:
            stages['financials'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}

        now = clock()
        post_focus = choose_actionable_focus(
            repository, store, as_of=now,
            expected_session=effective_session, verified_close_at=effective_close,
            limit=min(5, archive_pages),
        )
        focus_symbols_list = [item['symbol'] for item in post_focus['selected']]
        store.put('research_focus', 'ALL', {
            'symbols': focus_symbols_list, 'limit': min(5, archive_pages),
            'ready_pool_count': len(post_focus['ready_pool']), 'mode': 'ACTIONABLE_COMMITTEE',
        }, observed_at=now)
        stages['research_focus'] = {
            'symbols': focus_symbols_list,
            'limit': min(5, archive_pages),
            'ready_pool_count': len(post_focus['ready_pool']),
        }
        try:
            stages['official_events'] = review_official_contracts(store, api_key=dart_key, as_of=clock(), clock=clock)
        except (ProviderError, ValueError) as exc:
            stages['official_events'] = {'status': 'UNAVAILABLE', 'error_code': exc.code if isinstance(exc, ProviderError) else str(exc)}
        stages['prices'] = {k: check.get(k) for k in ['status', 'target_date', 'market_dates', 'error_code', 'missing_market']} if check else {'status': 'UNAVAILABLE'}
        if check and check.get('research_status'):
            stages['prices']['status'] = check['research_status']
        if not ready:
            stages['prices']['status'] = 'FALLBACK_LATEST_SESSION' if fallback_session else 'VERIFY_PENDING'
            if fallback_session:
                stages['prices']['fallback_session'] = fallback_session.isoformat()
        stages['prices']['required_session'] = session.isoformat() if session else None

        def build_report(as_of):
            return build_recommendations(
                repository, store, as_of=as_of,
                expected_session=session, verified_close_at=closed,
                price_refresh_ready=ready, actionable=True,
                fallback_price_session=fallback_session, actionable_focus=post_focus,
            )

        news_focus = list(dict.fromkeys(focus_symbols_list + post_focus['ready_pool']))
        report, stages['archive'], now = _refresh_actionable_news(
            repository, store, batch, news_focus, build_report=build_report, clock=clock,
        )
        stages['news_budget'] = {'reserved_calls': batch.reserved_calls, 'actual_calls': batch.actual_calls}
        report['refresh_stages'] = stages
        import hashlib, json
        report.pop('report_id', None)
        report['report_id'] = 'UR-' + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
        store.put('unified_report', 'ALL', report, observed_at=now)
        return report

    def focus_symbols():
        selected = choose_focus(repository, store, as_of=clock())
        store.put('research_focus', 'ALL', {'symbols': selected, 'limit': 10}, observed_at=clock())
        return selected
    focus_symbols()
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


def shared_calendar(client_id, client_secret, *, fetch_json=None):
    """Use the already installed common cache; never fall back to independent OAuth."""
    import os
    if not os.getenv('STOCK_TOSS_AUTH_CACHE_DIR'):
        raise ValueError('SHARED_AUTH_CACHE_CONFIGURATION_REQUIRED')
    if '/opt/stock-shared-auth' not in sys.path:
        sys.path.insert(0, '/opt/stock-shared-auth')
    from shared_toss_auth import SharedTossMarketDataClient
    from .daily_requests import DailyRefreshRequests
    requests = DailyRefreshRequests()
    return SharedTossMarketDataClient(
        client_id,
        client_secret,
        fetch_json=fetch_json or requests.get_json,
        post_form=requests.post_form_json,
    )
