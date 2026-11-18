"""Resumable provider-index archive; pagination limits never imply no bad news."""
from datetime import datetime, timedelta, timezone

from .dart_research import _year_start
from .models import Evidence, to_json_value
from .news_discovery import _matches
from .news_policy import news_windows
from .unified_research import KST, financial_universe
from .validation import validate_analysis_time


def collect_archive(repository, store, client, *, symbols=None, pages=5, clock=None, recent=False):
    if type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError("archive batch must reserve one to five calls")
    if type(recent) is not bool:
        raise ValueError('recent must be boolean')
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = validate_analysis_time(clock())
    end = now.astimezone(KST).date() - timedelta(days=1)
    start = news_windows(now)['recent_start'] if recent else _year_start(end, 3)
    cursor_kind = 'recent_archive_cursor' if recent else 'archive_cursor'
    eligible = [(s, f) for s, f, _ in financial_universe(repository, store, as_of=now)
                if f.gate == "PASS" and f.financial_score >= 70 and (symbols is None or s.symbol in symbols)]
    states = store.latest_by_symbol(cursor_kind, as_of=now)
    def priority(item):
        record=states.get(item[0].symbol)
        status=record['payload'].get('status') if record else None
        group=0 if status=='PARTIAL' else 1 if record is None else 3 if status=='PROVIDER_LIMIT' else 2
        return group,(record or {}).get('observed_at',''),-item[1].financial_score,item[0].symbol
    eligible.sort(key=priority)
    def due(symbol):
        record=states.get(symbol)
        if record is None:return True
        state=record['payload'];age=now-datetime.fromisoformat(record['observed_at'])
        if state.get('status')=='PROVIDER_LIMIT':return age>=timedelta(days=7)
        return state.get('status')=='PARTIAL' or state.get('end')!=end.isoformat() or age>=timedelta(hours=1)
    due_securities = [s for s, _ in eligible if due(s.symbol)]
    if not due_securities:
        return {"status": "CURRENT", "requested": 0}

    # A busy issuer can consume the accessible window without reaching three years.
    # Keep resuming existing work, but reserve room for unstarted issuers in this
    # same five-call budget so two partial searches cannot monopolize every run.
    unstarted = [s for s in due_securities if s.symbol not in states]
    partial = [s for s in due_securities if (states.get(s.symbol) or {}).get("payload", {}).get("status") == "PARTIAL"]
    if unstarted and partial and pages > 1:
        chosen = partial[:min(2, pages - 1)]
        chosen += unstarted[:pages - len(chosen)]
        selected = {s.symbol for s in chosen}
        chosen += [s for s in due_securities if s.symbol not in selected][:pages - len(chosen)]
    else:
        chosen = due_securities[:pages]

    client.archive_batch(pages)
    results = []
    used = 0
    for index, security in enumerate(chosen):
        # Allocate remaining calls evenly; an early completion frees calls for
        # subsequent targets without another cost/cooldown reservation.
        allocation = (pages - used) // (len(chosen) - index)
        result = _collect_issuer(store, client, security,
                                 old=(states.get(security.symbol) or {}).get("payload", {}),
                                 start=start, end=end, pages=allocation, clock=clock, recent=recent)
        used += result["actual_calls"]
        results.append(result)
    if len(results) == 1:
        return results[0]
    status = ("PARTIAL" if any(r["status"] == "PARTIAL" for r in results) else
              "PROVIDER_LIMIT" if any(r["status"] == "PROVIDER_LIMIT" for r in results) else "COMPLETE")
    return {"status": status, "actual_calls": used, "reserved_calls": pages,
            "requested": len(results), "results": results, "scope": results[0]['scope'],
            "history_review_complete": all(r["history_review_complete"] for r in results)}


def _collect_issuer(store, client, chosen, *, old, start, end, pages, clock, recent):
    cursor_kind = 'recent_archive_cursor' if recent else 'archive_cursor'
    article_kind = 'recent_archive_article' if recent else 'archive_article'
    archive_kind = 'recent_news_archive' if recent else 'news_archive'
    scope = 'NAVER_RECENT_NEWS_QUERY_RESULTS' if recent else 'NAVER_INDEXED_QUERY_RESULTS'
    state = dict(old) if old.get("status") == "PARTIAL" else {
        "next_start": 1, "anchor_url": None, "start": start.isoformat(), "end": end.isoformat(), "status": "PARTIAL",
        "refresh_boundary": old.get("newest_seen") if old.get("status") == "COMPLETE" else None,
        "baseline_complete": old.get("status") == "COMPLETE", "query": chosen.name}
    used, mismatches = 0, 0
    for _ in range(pages):
        position = state.get('probe_start',state["next_start"])
        page = client.archive_page(query=chosen.name, start=position)
        used += 1
        inputs = []
        for article in page.articles:
            if _matches(article.title, chosen.name, article.title.casefold()):
                e = Evidence("NEWS", article.title, article.url, article.published_at, article.observed_at, False,
                             (f"ISSUER:{chosen.symbol}", "NAVER_INDEXED_QUERY_RESULT"))
                inputs.append((article_kind, chosen.symbol, {"evidence": to_json_value(e), "description": article.description}))
        store.put_many(inputs, observed_at=page.observed_at)
        if position != 1 and state["anchor_url"] and state['anchor_url'] not in {a.url for a in page.articles}:
            # If the whole page is newer, seek the old anchor within the same bounded provider window.
            old_tail=datetime.fromisoformat(state['oldest_seen']) if state.get('oldest_seen') else None
            if old_tail and page.articles and page.articles[-1].published_at>old_tail and page.raw_count>=2:
                probe=position+page.raw_count-1
                if probe>=1000:
                    state.update(status='PROVIDER_LIMIT',limit_reason='ANCHOR_OUTSIDE_PROVIDER_WINDOW')
                else:state['probe_start']=probe
            else:
                state.pop('probe_start',None)
                state.update(next_start=1, anchor_url=None, status="PARTIAL")
                mismatches += 1
            store.put(cursor_kind, chosen.symbol, state, observed_at=clock())
            if state['status']=='PROVIDER_LIMIT':break
            continue
        state.pop('probe_start',None)
        if position == 1 and page.articles:
            state["newest_seen"] = page.articles[0].published_at.isoformat()
        oldest = page.articles[-1].published_at if page.articles else None
        exhausted = page.raw_count < 100 and position + page.raw_count - 1 >= page.total
        reached = bool(oldest and oldest.astimezone(KST).date().isoformat() < state['start'])
        refresh_done = bool(state.get("baseline_complete") and oldest and state.get("refresh_boundary")
                            and oldest < datetime.fromisoformat(state["refresh_boundary"]))
        if reached or exhausted or refresh_done:
            state["status"] = "COMPLETE"
        elif page.raw_count < 2 or position + page.raw_count - 1 >= 1000:
            state["status"] = "PROVIDER_LIMIT"
        else:
            state.update(next_start=position + page.raw_count - 1, anchor_url=page.articles[-1].url)
        state["scope"] = scope
        state["oldest_seen"] = oldest.isoformat() if oldest else None
        state["provider_total"] = page.total
        key = store.put(cursor_kind, chosen.symbol, state, observed_at=clock())
        if state["status"] != "PARTIAL":
            break
    if state["status"] == "COMPLETE":
        observed = clock()
        store.put(archive_kind, chosen.symbol, {"symbol": chosen.symbol, "start": state['start'], "end": state['end'],
                  "search_description": "Naver issuer query ordered by date, overlapping pages to cutoff/exhaustion",
                  "search_complete": True, "evidence": [], "scope": state["scope"], "archive_cursor_input_id": key}, observed_at=observed)
        if recent:
            store.put('recent_news_coverage', chosen.symbol, {'symbol': chosen.symbol, 'start': state['start'], 'end': state['end'],
                      'observed_at': observed.isoformat(), 'reviewed_at': observed.isoformat(), 'status': 'AUTO_SEARCH_VERIFIED',
                      'scope': scope, 'window_days': 90, 'sources': ['RECENT_NEWS_ARCHIVE'], 'source_refs': [key],
                      'reviewer': 'recent-news-pagination-v1', 'rationale': '90-day indexed query traversal; risk review is independent'},
                      observed_at=observed)
        dart = store.latest("dart_cursor", chosen.symbol, as_of=observed)
        if not recent and dart and dart["payload"]["start"] <= _year_start(end, 10).isoformat() and dart["payload"]["end"] >= state['end']:
            store.put("news_coverage", chosen.symbol, {"symbol": chosen.symbol, "start": state['start'], "end": state['end'],
                      "observed_at": observed.isoformat(), "reviewed_at": observed.isoformat(), "status": "AUTO_SEARCH_VERIFIED",
                      "scope": state["scope"], "sources": ["NEWS_ARCHIVE", "DART_HISTORY"], "source_refs": [key, dart["input_id"]],
                      "reviewer": "archive-pagination-v1", "rationale": "source traversal verified; material events remain separate reviews"},
                      observed_at=observed)
    return {"symbol": chosen.symbol, "status": state["status"], "actual_calls": used, "reserved_calls": pages,
            "pagination_restarts": mismatches, "oldest_seen": state.get("oldest_seen"), "scope": state.get("scope"),
            "next_start": state.get("next_start"), "history_review_complete": state["status"] == "COMPLETE"}
