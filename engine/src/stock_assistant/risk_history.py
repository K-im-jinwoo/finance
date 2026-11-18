"""Join a completed targeted history with observed recent news and official filings."""
from datetime import date, datetime, timedelta

from .news_policy import news_windows, RISK_QUERY_PLAN, RISK_QUERY_VERSION


def risk_coverage_payload(store, symbol, *, as_of):
    windows = news_windows(as_of)
    history = store.latest('risk_archive_cursor', symbol, as_of=as_of)
    dart = store.latest('dart_cursor', symbol, as_of=as_of)
    if not history or not dart:
        return None
    state, official = history['payload'], dart['payload']
    plan = {(q.get('keyword'), q.get('category')) for q in state.get('queries', [])}
    if (state.get('status') != 'COMPLETE' or state.get('query_version') != RISK_QUERY_VERSION
            or plan != set(RISK_QUERY_PLAN) or len(state.get('queries', [])) != len(RISK_QUERY_PLAN)
            or any(q.get('status') != 'COMPLETE' for q in state['queries'])
            or date.fromisoformat(state['start']) > windows['risk_start']
            or date.fromisoformat(official['start']) > windows['official_start']
            or date.fromisoformat(official['end']) < windows['end']):
        return None
    refs = [history, dart]
    sources = ['RISK_SEARCH', 'DART_HISTORY']
    history_end = date.fromisoformat(state['end'])
    if history_end < windows['end']:
        recent = store.latest('recent_archive_cursor', symbol, as_of=as_of)
        if not recent:
            return None
        current = recent['payload']
        if (current.get('status') != 'COMPLETE' or current.get('scope') != 'NAVER_RECENT_NEWS_QUERY_RESULTS'
                or date.fromisoformat(current['start']) > history_end + timedelta(days=1)
                or date.fromisoformat(current['end']) < windows['end']):
            return None
        refs.append(recent)
        sources.append('RECENT_NEWS_ARCHIVE')
    observed = max(datetime.fromisoformat(r['observed_at']) for r in refs)
    if observed > as_of:
        raise ValueError('risk coverage cannot use future observations')
    return {'symbol': symbol, 'start': state['start'], 'end': windows['end'].isoformat(),
            'observed_at': observed.isoformat(), 'reviewed_at': observed.isoformat(),
            'status': 'AUTO_SEARCH_VERIFIED', 'scope': 'NAVER_MATERIAL_QUERIES_AND_DART',
            'query_version': RISK_QUERY_VERSION, 'sources': sources, 'source_refs': [r['input_id'] for r in refs],
            'reviewer': 'split-risk-coverage-v1',
            'rationale': 'specified three-year risk queries and ten-year DART traversal; recent archive covers any date gap; event review remains separate'}


def refresh_risk_coverage(store, symbols, *, as_of):
    completed = []
    for symbol in symbols:
        payload = risk_coverage_payload(store, symbol, as_of=as_of)
        if payload:
            key = store.put('risk_coverage', symbol, payload, observed_at=as_of)
            completed.append({'symbol': symbol, 'input_id': key})
    return completed
