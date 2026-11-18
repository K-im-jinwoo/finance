"""Score reviewed events, not repeated headlines; preserve unresolved older risks."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from urllib.parse import urlsplit

from .identifiers import require_korean_security_symbol
from .models import Evidence
from .news_policy import news_windows, RISK_QUERY_VERSION
from .validation import assert_point_in_time, validate_analysis_time

CATEGORIES = {"CONTRACT", "APPROVAL", "POLICY", "EARNINGS", "DILUTION", "MANAGEMENT", "BUSINESS_DAMAGE"}
MATERIALITY = {"LOW": 5, "MEDIUM": 10, "HIGH": 20}
_RISK_WORDS = {"MANAGEMENT": ("횡령", "배임", "감사의견거절", "감사의견 거절"),
               "DILUTION": ("유상증자", "전환사채", "신주인수권부사채"),
               "BUSINESS_DAMAGE": ("계약해지", "계약 해지", "거래정지", "상장폐지", "회생절차")}
KST = timezone(timedelta(hours=9))


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("research timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ReviewedEvent:
    symbol: str
    event_id: str
    category: str
    direction: str
    state: str
    materiality: str
    verified: bool
    hard_exclusion: bool
    announced_at: datetime
    reviewed_at: datetime
    valid_until: datetime | None
    reviewer: str
    rationale: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self):
        require_korean_security_symbol(self.symbol)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", self.event_id):
            raise ValueError("event_id must be a stable issuer-event key")
        if (self.category not in CATEGORIES or self.direction not in {"POSITIVE", "NEGATIVE", "NEUTRAL"}
                or self.state not in {"ACTIVE", "RESOLVED", "CANCELLED"} or self.materiality not in MATERIALITY):
            raise ValueError("invalid reviewed event classification")
        if type(self.verified) is not bool or type(self.hard_exclusion) is not bool:
            raise ValueError("event verification flags must be booleans")
        for when in (self.announced_at, self.reviewed_at):
            validate_analysis_time(when)
        if (not self.evidence or not self.reviewer.strip() or not self.rationale.strip()
                or self.reviewed_at < self.announced_at
                or max(e.observed_at for e in self.evidence) > self.reviewed_at):
            raise ValueError("event review requires observed sources, reviewer and rationale")
        if self.valid_until:
            if self.valid_until.tzinfo is None:
                raise ValueError("event expiry must be timezone-aware")
            if self.valid_until < self.announced_at:
                raise ValueError("event validity ends before announcement")
        if self.direction == "POSITIVE" and self.valid_until is None:
            raise ValueError("positive events require an explicit expiry")
        if self.hard_exclusion and (not self.verified or self.direction != "NEGATIVE"):
            raise ValueError("hard exclusion requires a verified negative event")
        if self.verified and not any(e.official and f"ISSUER:{self.symbol}" in e.facts
                and urlsplit(e.url).scheme == "https" and urlsplit(e.url).hostname in {
                "dart.fss.or.kr", "opendart.fss.or.kr", "kind.krx.co.kr", "www.scourt.go.kr", "scourt.go.kr"}
                for e in self.evidence):
            raise ValueError("verified event requires issuer-bound official primary evidence")


def event_from_dict(p):
    from .repository import _evidence_from_dict
    return ReviewedEvent(p["symbol"], p["event_id"], p["category"], p["direction"], p["state"],
                         p["materiality"], p["verified"], p["hard_exclusion"], instant(p["announced_at"]),
                         instant(p["reviewed_at"]), instant(p["valid_until"]) if p.get("valid_until") else None,
                         p["reviewer"], p["rationale"], tuple(_evidence_from_dict(e) for e in p["evidence"]))


def risk_signals(evidence, *, as_of, reviewed_urls=()):
    """Headlines can request review; they cannot prove guilt or earn positive points."""
    reviewed, seen, results = set(reviewed_urls), set(), []
    for item in evidence:
        assert_point_in_time(as_of, published_at=item.published_at, observed_at=item.observed_at, label="news source")
        if item.url in seen:
            continue
        title = item.title.replace(" ", "")
        for category, words in _RISK_WORDS.items():
            cutoff = timedelta(days=365 * (10 if category == "MANAGEMENT" else 5))
            if (category, item.url) in reviewed:
                continue
            if as_of - item.published_at <= cutoff and (any(word.replace(" ", "") in title for word in words)
                    or f'MATERIAL_QUERY:{category}' in item.facts):
                results.append({"category": category, "url": item.url, "reason": "MATERIAL_EVENT_REVIEW_REQUIRED"})
                seen.add(item.url)
                break
    return results


def score_news(symbol, events, coverage, *, as_of, raw_evidence=(), risk_coverage=None):
    as_of = validate_analysis_time(as_of)
    require_korean_security_symbol(symbol)
    latest = {}
    for event in events:
        if event.symbol != symbol:
            raise ValueError("news event issuer does not match")
        assert_point_in_time(as_of, published_at=event.announced_at, observed_at=event.reviewed_at, label="event review")
        for source in event.evidence:
            assert_point_in_time(as_of, published_at=source.published_at, observed_at=source.observed_at, label="event evidence")
        old = latest.get(event.event_id)
        if old and old.reviewed_at == event.reviewed_at and old != event:
            raise ValueError("conflicting reviews for the same event version")
        if old is None or event.reviewed_at > old.reviewed_at:
            latest[event.event_id] = event
    pending, exclusions, contributions = [], [], []
    covered = False
    if coverage:
        if coverage["symbol"] != symbol:
            raise ValueError("news coverage issuer does not match")
        observed, reviewed = instant(coverage["observed_at"]), instant(coverage["reviewed_at"])
        start, end = date.fromisoformat(coverage["start"]), date.fromisoformat(coverage["end"])
        if start > end or end > as_of.astimezone(KST).date() or observed > reviewed or reviewed > as_of:
            raise ValueError("invalid news coverage dates")
        windows = news_windows(as_of)
        required_end, required_start = windows['end'], windows['recent_start']
        recent_scope = coverage.get('scope') == 'NAVER_RECENT_NEWS_QUERY_RESULTS'
        acceptable = coverage.get("status") == "REVIEWED" or (coverage.get("status") == "AUTO_SEARCH_VERIFIED"
                      and coverage.get("scope") in {"NAVER_INDEXED_QUERY_RESULTS", 'NAVER_RECENT_NEWS_QUERY_RESULTS'})
        covered = (acceptable and coverage.get("reviewer", "").strip()
                   and coverage.get("source_refs") and ({'RECENT_NEWS_ARCHIVE'} if recent_scope else {'NEWS_ARCHIVE', 'DART_HISTORY'}).issubset(coverage.get("sources", []))
                   and start <= required_start and end >= required_end
                   and as_of - reviewed <= timedelta(days=7))
    if not covered:
        pending.append("NEWS_RECENT_COVERAGE_UNAVAILABLE")
    windows = news_windows(as_of)
    risk_covered = False
    if risk_coverage:
        if risk_coverage['symbol'] != symbol:
            raise ValueError('risk coverage issuer does not match')
        observed, reviewed = instant(risk_coverage['observed_at']), instant(risk_coverage['reviewed_at'])
        start, end = date.fromisoformat(risk_coverage['start']), date.fromisoformat(risk_coverage['end'])
        if start > end or end > as_of.astimezone(KST).date() or observed > reviewed or reviewed > as_of:
            raise ValueError('invalid risk coverage dates')
        acceptable = risk_coverage.get('status') == 'REVIEWED' or (
            risk_coverage.get('status') == 'AUTO_SEARCH_VERIFIED'
            and risk_coverage.get('scope') == 'NAVER_MATERIAL_QUERIES_AND_DART'
            and risk_coverage.get('query_version') == RISK_QUERY_VERSION)
        risk_covered = bool(acceptable and risk_coverage.get('reviewer', '').strip()
                            and risk_coverage.get('source_refs')
                            and {'RISK_SEARCH', 'DART_HISTORY'}.issubset(risk_coverage.get('sources', []))
                            and start <= windows['risk_start'] and end >= windows['end']
                            and as_of - reviewed <= timedelta(days=7))
    elif coverage and coverage.get('status') == 'REVIEWED':
        # Retain explicitly imported, complete manual three-year reviews. An
        # automatic recent search or declaration of coverage cannot substitute.
        risk_covered = bool(covered and date.fromisoformat(coverage['start']) <= windows['risk_start']
                            and {'NEWS_ARCHIVE', 'DART_HISTORY'}.issubset(coverage.get('sources', [])))
    if not risk_covered:
        pending.append('RISK_HISTORY_COVERAGE_UNAVAILABLE')
    positive, negative, penalty = 0, 0, 0
    reviewed_urls, scored_sources = [], {}
    for key, event in sorted(latest.items()):
        if not event.verified:
            if event.direction == "NEGATIVE":
                pending.append("UNVERIFIED_NEGATIVE_EVENT:" + key)
            contributions.append({"event_id": key, "points": 0, "status": "UNVERIFIED"})
            continue
        official_urls = {e.url for e in event.evidence if e.official}
        if any((event.category, url) in scored_sources for url in official_urls):
            raise ValueError("duplicate official event evidence must use the same event_id")
        for url in official_urls:
            scored_sources[(event.category, url)] = key
        reviewed_urls.extend((event.category, e.url) for e in event.evidence)
        if event.state != "ACTIVE":
            contributions.append({"event_id": key, "points": 0, "status": event.state})
            continue
        if event.direction == "NEGATIVE":
            # Unresolved negative reviews are retained even if older than the search window.
            pending.append('UNRESOLVED_NEGATIVE_EVENT:' + key)
            if event.hard_exclusion:
                exclusions.append("CONFIRMED_HARD_RISK:" + key)
            elif event.category in {"DILUTION", "MANAGEMENT"}:
                penalty += MATERIALITY[event.materiality]
            else:
                negative += MATERIALITY[event.materiality]
            contributions.append({"event_id": key, "points": -MATERIALITY[event.materiality], "status": "ACTIVE_RISK",
                                  "score_target": "risk_penalty" if event.category in {"DILUTION", "MANAGEMENT"} else "news_score"})
        elif event.direction == "POSITIVE":
            if event.valid_until < as_of:
                contributions.append({"event_id": key, "points": 0, "status": "EXPIRED"})
            elif event.category == "EARNINGS":
                contributions.append({"event_id": key, "points": 0, "status": "FINANCIAL_DOUBLE_COUNT_PREVENTED"})
            elif not windows['recent_start'] <= event.announced_at.astimezone(KST).date() <= windows['end']:
                contributions.append({'event_id': key, 'points': 0, 'status': 'OUTSIDE_RECENT_WINDOW'})
            else:
                positive += MATERIALITY[event.materiality]
                contributions.append({"event_id": key, "points": MATERIALITY[event.materiality], "status": "VERIFIED_ACTIVE"})
    signals = risk_signals(raw_evidence, as_of=as_of, reviewed_urls=reviewed_urls)
    pending.extend("MATERIAL_EVENT_REVIEW_REQUIRED:" + item["url"] for item in signals)
    score = Decimal(max(0, min(100, 50 + min(50, positive) - min(50, negative)))) if covered else None
    return {"news_score": score, "risk_penalty": Decimal(min(30, penalty)),
            "coverage_complete": bool(covered), 'recent_coverage_complete': bool(covered),
            'risk_coverage_complete': risk_covered, "pending_checks": tuple(dict.fromkeys(pending)),
            "hard_exclusions": tuple(exclusions), "events": contributions, "review_signals": signals}
