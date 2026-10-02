"""News leads are a due-diligence queue, never financial facts or buy signals."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AssetType, Security
from .providers.news import NewsArticle


_TAG_PREFIX = re.compile(r"^(?:\[[^\]]{1,40}\]\s*){0,3}")
_NOISE = ("[포토]", "[인사]", "[부고]", "종목 추천", "목표주가", "주가 전망")
_UNCONFIRMED_OR_NEGATIVE = (
    "취소", "해지", "부인", "오보", "루머", "불발", "감소", "적자", "악화",
    "기대", "전망", "가능성", "추정", "검토", "추진", "미확정", "협의", "논의",
    "mou", "업무협약", "사실무근", "없다", "미체결", "불확실",
    "신청", "예정",
)
_CONTRADICTIONS = ("취소", "해지", "부인", "오보", "불발", "사실무근", "미체결")
_EVENTS = (
    ("CONTRACT", ("수주", "공급계약", "판매계약", "계약 체결"), ()),
    ("EARNINGS", ("실적", "영업이익", "흑자전환"), ("증가", "개선", "최대", "흑자전환", "호실적")),
    ("APPROVAL", ("허가", "승인", "인증"), ("획득", "취득", "완료", "확정", "통과", "승인")),
    ("POLICY", ("정책", "정부", "지원"), ("수혜", "선정", "확정")),
)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("news timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _matches(title: str, name: str, lowered: str) -> bool:
    name = name.strip()
    if len(name) < 2 or name.casefold() not in lowered:
        return False
    matched = False
    for match in re.finditer(re.escape(name), title, flags=re.IGNORECASE):
        if match.start() and title[match.start() - 1].isalnum():
            continue
        suffix = title[match.end():]
        # Unicode letters include Hanja abbreviations such as HD현대重.
        if suffix and suffix[0].isalnum() and not any(
            suffix.startswith(particle)
            and (len(suffix) == len(particle) or not suffix[len(particle)].isalnum())
            for particle in ("은", "는", "이", "가", "의", "와", "과", "도", "에서", "에", "로", "부터")
        ):
            continue
        matched = True
        break
    if not matched:
        return False
    # Very short issuer names also occur as ordinary words. Require a subject marker.
    if len(name) <= 2:
        subject = _TAG_PREFIX.sub("", title)
        return bool(re.match(rf"{re.escape(name)}\s*[,，'‘\"“(]", subject, re.IGNORECASE))
    return True


def discover_news(articles: list[NewsArticle], securities: list[Security], *,
                  as_of: datetime, lookback_hours: int = 48) -> dict:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int) or not 1 <= lookback_hours <= 72:
        raise ValueError("lookback_hours must be between 1 and 72")
    if len(articles) > 500:
        raise ValueError("news collection cannot exceed 500 articles")
    now = as_of.astimezone(timezone.utc)
    local_day = now.astimezone(timezone(timedelta(hours=9))).date()
    universe = [s for s in securities if s.asset_type is AssetType.COMMON
                and (s.listed_on is None or s.listed_on <= local_day)
                and (s.delisted_on is None or s.delisted_on > local_day)]
    counts = {k: 0 for k in ("stale", "future", "duplicate", "noise", "unmatched", "ambiguous", "no_material")}
    seen_urls, seen_titles, events = set(), set(), []
    # A stable order makes overlaps across query results deterministic.
    for article in sorted(articles, key=lambda a: (-a.published_at.timestamp(), a.url, a.title)):
        if article.published_at.tzinfo is None or article.observed_at.tzinfo is None:
            raise ValueError("news timestamps must be timezone-aware")
        if article.published_at > now or article.observed_at > now or article.published_at > article.observed_at:
            counts["future"] += 1
            continue
        if article.published_at < now - timedelta(hours=lookback_hours):
            counts["stale"] += 1
            continue
        title_key = re.sub(r"[^0-9a-z가-힣]", "", article.title.casefold())
        if article.url in seen_urls or title_key in seen_titles:
            counts["duplicate"] += 1
            continue
        seen_urls.add(article.url)
        seen_titles.add(title_key)
        lowered = article.title.casefold()
        matches = [s for s in universe if _matches(article.title, s.name, lowered)]
        if len(matches) != 1:
            counts["ambiguous" if matches else "unmatched"] += 1
            continue
        material_mentioned = any(t in lowered for _, triggers, _ in _EVENTS for t in triggers)
        contradictory = material_mentioned and any(w in lowered for w in _CONTRADICTIONS)
        if contradictory:
            category = "NEWS_RISK"
        else:
            if any(w in lowered for w in (*_NOISE, *_UNCONFIRMED_OR_NEGATIVE)
                   if not (w == "적자" and "흑자전환" in lowered)):
                counts["noise"] += 1
                continue
            category = next((name for name, triggers, confirmations in _EVENTS
                             if any(t in lowered for t in triggers)
                             and (not confirmations or any(t in lowered for t in confirmations))), None)
        if category is None:
            counts["no_material"] += 1
            continue
        security = matches[0]
        identity = f"{security.symbol}|{category}|{article.url}|{article.published_at.isoformat()}"
        events.append({
            "event_key": "N-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "symbol": security.symbol, "name": security.name, "category": category,
            "title": article.title, "url": article.url, "description": article.description,
            "published_at": article.published_at.astimezone(timezone.utc).isoformat(),
            "observed_at": article.observed_at.astimezone(timezone.utc).isoformat(),
            "source": article.source, "timestamp_basis": article.timestamp_basis,
            "official": False,
        })
    return {"observed_at": now.isoformat(), "status": "OK", "lookback_hours": lookback_hours,
            "source": "NAVER_NEWS_SEARCH", "coverage": "BOUNDED_KEYWORD_SEARCH",
            "article_count": len(articles), "filter_counts": counts, "events": events}


class NewsDiscoveryStore:
    """Additive tables initialized only when the opt-in collector is used."""
    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS news_discovery_runs (
                    run_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS news_discovery_events (
                    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, published_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS news_discovery_events_time
                    ON news_discovery_events(published_at,first_seen_at);
            """)

    def save(self, run: dict) -> None:
        observed = _instant(run["observed_at"])
        for event in run.get("events", []):
            if _instant(event["published_at"]) > _instant(event["observed_at"]) or _instant(event["observed_at"]) > observed:
                raise ValueError("future news evidence cannot be persisted")
        payload = json.dumps(run, ensure_ascii=False, sort_keys=True)
        run_id = hashlib.sha256(payload.encode()).hexdigest()
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            for event in run.get("events", []):
                connection.execute("INSERT OR IGNORE INTO news_discovery_events VALUES(?,?,?,?,?)", (
                    event["event_key"], event["symbol"], event["published_at"],
                    observed.isoformat(), json.dumps(event, ensure_ascii=False, sort_keys=True),
                ))
            connection.execute("INSERT OR IGNORE INTO news_discovery_runs VALUES(?,?,?)",
                               (run_id, observed.isoformat(), payload))


def latest_news_discovery(database_path: Path, *, as_of: datetime, limit: int = 10) -> dict:
    """Read without migrating a legacy DB; failed refreshes never look like empty success."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("news candidate limit must be between 1 and 20")
    empty = {"status": "UNAVAILABLE", "reason": "NEWS_NOT_COLLECTED", "candidates": []}
    if not database_path.is_file():
        return empty
    now = as_of.astimezone(timezone.utc)
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='news_discovery_runs'").fetchone():
            return empty
        row = connection.execute("SELECT payload_json FROM news_discovery_runs WHERE observed_at<=? "
                                 "ORDER BY observed_at DESC,rowid DESC LIMIT 1", (now.isoformat(),)).fetchone()
        if row is None:
            return empty
        run = json.loads(row[0])
        if run["status"] != "OK":
            return {**empty, "reason": run.get("error_code", "NEWS_FETCH_FAILED"), "observed_at": run["observed_at"]}
        if now - _instant(run["observed_at"]) > timedelta(hours=12):
            return {**empty, "reason": "NEWS_REFRESH_STALE", "observed_at": run["observed_at"]}
        cutoff = (now - timedelta(hours=run["lookback_hours"])).isoformat()
        rows = connection.execute("SELECT payload_json,first_seen_at FROM news_discovery_events "
                                  "WHERE first_seen_at<=? AND published_at>=? AND published_at<=? "
                                  "ORDER BY published_at DESC,event_key", (now.isoformat(), cutoff, now.isoformat())).fetchall()
    # Revalidate old evidence without deleting the original stored observations.
    records = [(json.loads(payload), first_seen) for payload, first_seen in rows]
    records = [(event, first_seen) for event, first_seen in records
               if _matches(event["title"], event["name"], event["title"].casefold())]
    grouped, blocked = {}, {}
    for event, _ in records:
        if event["category"] == "NEWS_RISK":
            blocked[event["symbol"]] = max(blocked.get(event["symbol"], ""), event["published_at"])
    for event, first_seen in records:
        if event["category"] == "NEWS_RISK" or event["published_at"] <= blocked.get(event["symbol"], ""):
            continue
        if event["symbol"] not in grouped:
            grouped[event["symbol"]] = {"symbol": event["symbol"], "name": event["name"],
                                       "evidence": [], "status": "REVIEW_REQUIRED"}
        evidence = grouped[event["symbol"]]["evidence"]
        if len(evidence) < 3:
            evidence.append({**event, "first_seen_at": first_seen})
    return {"status": "OK", "observed_at": run["observed_at"], "lookback_hours": run["lookback_hours"],
            "source": run["source"], "coverage": run["coverage"],
            "candidates": list(grouped.values())[:limit], "matched_symbols": len(grouped),
            "article_count": run["article_count"], "filter_counts": run["filter_counts"]}
