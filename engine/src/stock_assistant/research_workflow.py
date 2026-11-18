"""Provider collection and explicit review import for the opt-in research contract."""
from datetime import date, datetime, timedelta, timezone

from .identifiers import require_korean_security_symbol
from .dart_research import _year_start, stored_filings
from .models import Evidence, to_json_value
from .news_discovery import _matches
from .news_research import event_from_dict, instant, score_news
from .news_policy import news_windows
from .risk_history import risk_coverage_payload
from .repository import _evidence_from_dict
from .unified_research import KST, financial_universe
from .validation import assert_point_in_time, validate_analysis_time


def parse_evidence(p):
    if type(p.get("official")) is not bool:
        raise ValueError("evidence official must be boolean")
    return Evidence(p["source_type"], p["title"], p["url"], instant(p["published_at"]),
                    instant(p["observed_at"]), p["official"], tuple(p.get("facts", [])))


def collect_targeted_news(repository, store, client, *, clock=None, limit=5):
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("targeted news limit must be between 1 and 5")
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = validate_analysis_time(clock())
    eligible = [(s, f) for s, f, _ in financial_universe(repository, store, as_of=now)
                if f.gate == "PASS" and f.financial_score >= 70]
    eligible.sort(key=lambda sf: ((store.latest("news_batch", sf[0].symbol, as_of=now) or {}).get("observed_at", ""),
                                 -sf[1].financial_score, sf[0].symbol))
    chosen, names = [], set()
    for s, f in eligible:
        if s.name in names or len(s.name) > 80:
            continue
        chosen.append(s)
        names.add(s.name)
        if len(chosen) == limit:
            break
    if not chosen:
        return {"requested": 0, "results": []}
    # Uses the existing shared daily/monthly budget and cooldown, without resetting it.
    articles = client.collect(observed_at=now, queries=tuple(s.name for s in chosen))
    observed = clock()
    results = []
    for security in chosen:
        evidence = []
        for article in articles:
            if _matches(article.title, security.name, article.title.casefold()):
                evidence.append(Evidence("NEWS", article.title, article.url, article.published_at,
                                         article.observed_at, False, (f"ISSUER:{security.symbol}",)))
        identity = store.put("news_batch", security.symbol,
                             {"query": security.name, "coverage": "BOUNDED_RECENT_SEARCH", "evidence": to_json_value(evidence)},
                             observed_at=max([observed] + [e.observed_at for e in evidence]))
        results.append({"symbol": security.symbol, "articles": len(evidence), "input_id": identity,
                        "history_review_complete": False})
    return {"requested": len(chosen), "results": results, "source_read_only": True}


def import_reviews(store, payload, *, as_of, clock=None):
    """Validate a whole document before persisting its reviewed evidence.

    Historical research cannot see this import before its actual ingestion time.
    Coverage must point to already imported archive and DART cursor records.
    """
    as_of = validate_analysis_time(as_of)
    ingested_at = validate_analysis_time((clock or (lambda: datetime.now(timezone.utc)))())
    if not isinstance(payload, dict) or set(payload) - {"events", "coverage", "archives", "fcf_reviews", 'recent_coverage', 'risk_coverage'}:
        raise ValueError("review document has unknown fields")
    if any(not isinstance(v, list) or len(v) > 1000 for v in payload.values()):
        raise ValueError("review document arrays must contain at most 1000 items")
    pending = []
    for p in payload.get("events", []):
        required = {"symbol", "event_id", "category", "direction", "state", "materiality", "verified", "hard_exclusion",
                    "announced_at", "reviewed_at", "reviewer", "rationale", "evidence"}
        if not isinstance(p, dict) or not required.issubset(p):
            raise ValueError("reviewed event is missing required fields")
        sources = tuple(parse_evidence(e) for e in p["evidence"])
        event = event_from_dict({**p, "evidence": to_json_value(sources)})
        assert_point_in_time(as_of, published_at=event.announced_at, observed_at=event.reviewed_at, label="imported review")
        prior = [event_from_dict(r["payload"]) for r in store.records("reviewed_event", event.symbol, as_of=as_of)
                 if r["payload"]["event_id"] == event.event_id]
        if prior:
            last = max(prior, key=lambda e: e.reviewed_at)
            if event.reviewed_at < last.reviewed_at or (event.reviewed_at == last.reviewed_at and event != last):
                raise ValueError("event review cannot overwrite or rewind an existing version")
            if event.state != last.state and max(e.published_at for e in event.evidence) <= max(e.published_at for e in last.evidence):
                raise ValueError("event resolution/cancellation requires newer primary evidence")
        pending.append(("reviewed_event", event.symbol, to_json_value(event)))
    for p in payload.get("archives", []):
        require_korean_security_symbol(p["symbol"])
        start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
        if (start > end or end > as_of.date() or not p.get("search_description", "").strip()
                or type(p.get("search_complete")) is not bool):
            raise ValueError("archive requires bounded dates and an explicit search record")
        evidence = [parse_evidence(e) for e in p["evidence"]]
        if any(not start <= e.published_at.date() <= end or e.observed_at > as_of for e in evidence):
            raise ValueError("archive evidence is outside its declared search window")
        pending.append(("news_archive", p["symbol"], {**p, "evidence": to_json_value(evidence)}))
    for p in payload.get("fcf_reviews", []):
        require_korean_security_symbol(p["symbol"])
        evidence = parse_evidence(p["evidence"])
        assert_point_in_time(as_of, observed_at=evidence.observed_at, published_at=evidence.published_at, label="FCF review")
        if (f"ISSUER:{p['symbol']}" not in evidence.facts or not p.get("reviewer", "").strip()
                or not p.get("rationale", "").strip()):
            raise ValueError("FCF review requires issuer-bound evidence and review rationale")
        pending.append(("fcf_review", p["symbol"], to_json_value(evidence)))
    for p in payload.get("coverage", []):
        # Resolve references to real stored search inputs; a declaration alone is insufficient.
        score_news(p["symbol"], [], p, as_of=as_of)
        references = [store.get(key, as_of=as_of) for key in p.get("source_refs", [])]
        if not references or any(r is None or r["symbol"] != p["symbol"] for r in references):
            raise ValueError("coverage source references must exist for the same issuer")
        archives = [r["payload"] for r in references if r["kind"] == "news_archive"]
        dart = [r["payload"] for r in references if r["kind"] == "dart_cursor"]
        if not any(a["search_complete"] and a["start"] <= p["start"] and a["end"] >= p["end"] for a in archives):
            raise ValueError("coverage lacks a completed archive search for its date range")
        ten_year_start = _year_start(as_of.astimezone(KST).date() - timedelta(days=1), 10).isoformat()
        if not any(a["start"] <= ten_year_start and a["end"] >= p["end"] for a in dart):
            raise ValueError("coverage lacks ten-year DART risk history")
        pending.append(("news_coverage", p["symbol"], p))
    for p in payload.get('recent_coverage', []):
        score_news(p['symbol'], [], p, as_of=as_of)
        references = [store.get(key, as_of=as_of) for key in p.get('source_refs', [])]
        if not references or any(r is None or r['symbol'] != p['symbol'] for r in references):
            raise ValueError('recent coverage references must exist for the same issuer')
        if p.get('scope') != 'NAVER_RECENT_NEWS_QUERY_RESULTS' or p.get('status') != 'REVIEWED':
            raise ValueError('recent coverage requires an explicit reviewed recent search')
        if not any(r['kind'] in {'recent_archive_cursor', 'recent_news_archive'}
                   and (r['payload'].get('status') == 'COMPLETE' or r['payload'].get('search_complete') is True)
                   and r['payload']['start'] <= p['start'] and r['payload']['end'] >= p['end'] for r in references):
            raise ValueError('recent coverage lacks a completed ninety-day search')
        pending.append(('recent_news_coverage', p['symbol'], p))
    for p in payload.get('risk_coverage', []):
        score_news(p['symbol'], [], None, as_of=as_of, risk_coverage=p)
        actual = risk_coverage_payload(store, p['symbol'], as_of=as_of)
        if p.get('status') != 'REVIEWED' or actual is None:
            raise ValueError('risk coverage requires completed targeted history and official sources')
        if (set(p.get('source_refs', [])) != set(actual['source_refs'])
                or p['start'] < actual['start'] or p['end'] > actual['end']):
            raise ValueError('risk coverage exceeds its stored source references')
        pending.append(('risk_coverage', p['symbol'], p))
    # Detect conflicting versions and duplicate official event evidence within this import.
    for symbol in {symbol for kind, symbol, _ in pending if kind == "reviewed_event"}:
        events = [event_from_dict(p) for kind, s, p in pending if kind == "reviewed_event" and s == symbol]
        score_news(symbol, events, None, as_of=as_of)
    identities = store.put_many(pending, observed_at=ingested_at)
    result = [{"kind": kind, "symbol": symbol, "input_id": identity}
              for (kind, symbol, _), identity in zip(pending, identities)]
    return {"imported": len(result), "inputs": result, "source_read_only": True}


def collect_calendar(store, client, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = validate_analysis_time(clock())
    day = now.astimezone(KST).date()
    calendar = client.calendar(day)
    observed = clock()
    # The provider adapter validates dates and a previous regular market session.
    key = store.put("market_calendar", "KR", {"source": "TOSS_SECURITIES_OPEN_API", "date": day.isoformat(),
                                             "calendar": calendar}, observed_at=observed)
    return {"input_id": key, "previous_session": calendar["previousBusinessDay"]["date"]}


def market_session(store, *, as_of):
    record = store.latest("market_calendar", "KR", as_of=as_of)
    if record is None or record["payload"].get("date") != as_of.astimezone(KST).date().isoformat():
        return None, None
    today = record["payload"]["calendar"].get("today", {})
    regular = today.get("integrated", {}).get("regularMarket")
    if isinstance(regular, dict) and regular.get("endTime"):
        closed = instant(regular["endTime"])
        if closed.astimezone(KST).date().isoformat() != today.get("date"):
            raise ValueError("market close date does not match calendar session")
        if closed <= as_of:
            return date.fromisoformat(today["date"]), closed
    previous = record["payload"]["calendar"]["previousBusinessDay"]
    if not previous["integrated"].get("regularMarket"):
        return None, None
    return date.fromisoformat(previous["date"]), None


def expected_session(store, *, as_of):
    return market_session(store, as_of=as_of)[0]


def review_queue(repository, store, *, as_of, limit=25):
    """Expose evidence references and required windows for actual source review."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("review queue limit must be between 1 and 50")
    from .news_research import risk_signals
    eligible = [(s, f, b) for s, f, b in financial_universe(repository, store, as_of=as_of) if f.gate == "PASS"]
    eligible.sort(key=lambda item: (-item[1].financial_score, item[0].symbol))
    result = []
    for security, financial, bundle in eligible[:limit]:
        cursor = store.latest("dart_cursor", security.symbol, as_of=as_of)
        batches = store.records("news_batch", security.symbol, as_of=as_of)
        archives = store.records("news_archive", security.symbol, as_of=as_of)
        raw = stored_filings(store, security.symbol, as_of=as_of, cursor=cursor)
        for r in batches + archives:
            raw.extend(parse_evidence(e) for e in r["payload"]["evidence"])
        for kind in ('archive_article', 'material_archive_article', 'recent_archive_article', 'risk_archive_article'):
            for r in store.records(kind, security.symbol, as_of=as_of):
                raw.append(parse_evidence(r['payload']['evidence']))
        signals = risk_signals(raw, as_of=as_of)
        positive = [to_json_value(e) for e in raw if as_of - e.published_at <= timedelta(days=90)
                    and any(w in e.title for w in ("수주", "계약", "승인", "허가", "정책"))]
        result.append({"symbol": security.symbol, "name": security.name, "financial_score": to_json_value(financial.financial_score),
                       "financial_pending_checks": financial.pending_checks, "risk_review_signals": signals,
                       "recent_event_sources": positive, "dart_cursor_input_id": cursor["input_id"] if cursor else None,
                       "archive_input_ids": [r["input_id"] for r in archives],
                       "required_news_start": news_windows(as_of)['recent_start'].isoformat(),
                       'required_risk_start': news_windows(as_of)['risk_start'].isoformat(),
                       'required_official_start': news_windows(as_of)['official_start'].isoformat(),
                       'recent_cursor_input_id': (store.latest('recent_archive_cursor', security.symbol, as_of=as_of) or {}).get('input_id'),
                       'risk_cursor_input_id': (store.latest('risk_archive_cursor', security.symbol, as_of=as_of) or {}).get('input_id'),
                       "required_end": (as_of.astimezone(KST).date() - timedelta(days=1)).isoformat()})
    return {"source_read_only": True, "reviews": result, "events_automatically_verified": False}
