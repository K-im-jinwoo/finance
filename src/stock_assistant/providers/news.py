from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Evidence
from .http import AuthenticationError, RateLimitError, UpstreamSchemaError, get_json


_TAG_PATTERN = re.compile(r"<[^>]+>")
_SPACE_PATTERN = re.compile(r"\s+")
_TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def _clean_text(value: str) -> str:
    return _SPACE_PATTERN.sub(" ", _TAG_PATTERN.sub("", html.unescape(value))).strip()


def _canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    query = urlencode(sorted((key, val) for key, val in parse_qsl(parts.query) if key.casefold() not in _TRACKING_KEYS))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, query, ""))


def normalize_news_items(items: list[dict[str, Any]], *, observed_at: datetime) -> list[Evidence]:
    results: list[Evidence] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise UpstreamSchemaError(f"news item {index} must be an object")
        title = _clean_text(str(item.get("title", "")))
        raw_url = str(item.get("originallink") or item.get("url") or "")
        published_value = item.get("published_at")
        if not title or not raw_url or not published_value:
            raise UpstreamSchemaError(f"news item {index} is missing title, URL, or published_at")
        try:
            published_at = datetime.fromisoformat(str(published_value))
        except ValueError as exc:
            raise UpstreamSchemaError(f"news item {index} has invalid published_at") from exc
        url = _canonical_url(raw_url)
        title_key = re.sub(r"[^0-9A-Za-z가-힣]+", "", title).casefold()
        if url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title_key)
        results.append(Evidence(
            source_type=str(item.get("source_type", "NEWS")),
            title=title,
            url=url,
            published_at=published_at,
            observed_at=observed_at,
            official=bool(item.get("official", False)),
            facts=tuple(str(value) for value in item.get("facts", []) if str(value).strip()),
        ))
    return results


NAVER_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
DEFAULT_DISCOVERY_QUERIES = ("특징주", "공급계약", "흑자전환", "허가 승인", "정책 수혜")


@dataclass(frozen=True, slots=True)
class NewsArticle:
    title: str
    url: str
    published_at: datetime
    observed_at: datetime
    description: str = ""
    source: str = "NAVER_NEWS_SEARCH"
    timestamp_basis: str = "PROVIDER_PUBDATE"


def normalize_naver_news(payload: dict, *, observed_at: datetime) -> list[NewsArticle]:
    """Keep search snippets and provider times; never fetch or certify article bodies."""
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(payload, dict):
        raise UpstreamSchemaError("Naver news response must be an object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 100:
        raise UpstreamSchemaError("Naver news response requires at most 100 items")
    articles = []
    for item in items:
        if not isinstance(item, dict):
            raise UpstreamSchemaError("Naver news item must be an object")
        try:
            title = _clean_text(str(item.get("title", "")))
            url = _canonical_url(str(item.get("originallink") or item.get("link") or ""))
            parts = urlsplit(url)
            published = parsedate_to_datetime(str(item.get("pubDate", "")))
            if not title or len(url) > 2048 or not parts.hostname or parts.scheme not in {"https", "http"}:
                raise ValueError("invalid article identity")
            if parts.username or parts.password or published.tzinfo is None:
                raise ValueError("invalid article boundary")
        except (TypeError, ValueError, OverflowError) as exc:
            raise UpstreamSchemaError("Naver news item has invalid identity or pubDate") from exc
        articles.append(NewsArticle(
            title[:240], url, published.astimezone(timezone.utc),
            observed_at.astimezone(timezone.utc),
            _clean_text(str(item.get("description", "")))[:280],
        ))
    return articles


class NaverNewsClient:
    def __init__(self, client_id: str, client_secret: str, *, fetch_json: Callable = get_json,
                 clock: Callable | None = None, budget=None):
        if any(not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value)
               for value in (client_id, client_secret)):
            raise AuthenticationError("valid Naver client credential files are required")
        self._client_id, self._client_secret = client_id, client_secret
        self._fetch_json = fetch_json
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Lazy import keeps the shared provider exception definitions independent.
        from ..news_cost import NewsApiBudget
        self._budget = budget if budget is not None else NewsApiBudget()

    def collect(self, *, observed_at: datetime, queries=DEFAULT_DISCOVERY_QUERIES,
                display: int = 100) -> list[NewsArticle]:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if isinstance(display, bool) or not isinstance(display, int) or not 1 <= display <= 100:
            raise ValueError("display must be between 1 and 100")
        if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= 5:
            raise ValueError("one to five unique news queries are required")
        if any(not isinstance(q, str) or not q.strip() or len(q) > 80 for q in queries):
            raise ValueError("news query must contain 1 to 80 characters")
        if len(set(queries)) != len(queries):
            raise ValueError("one to five unique news queries are required")
        self._budget.reserve(len(queries), at=self._clock())
        articles = []
        for query in queries:
            try:
                response = self._fetch_json(
                    NAVER_NEWS_URL,
                    query={"query": query, "display": str(display), "start": "1", "sort": "date", "format": "json"},
                    headers={"X-NCP-APIGW-API-KEY-ID": self._client_id,
                             "X-NCP-APIGW-API-KEY": self._client_secret},
                    timeout_seconds=10,
                )
            except (RateLimitError, AuthenticationError):
                self._budget.halt_provider(at=self._clock())
                raise
            received_at = max(observed_at, self._clock())
            articles.extend(normalize_naver_news(response.payload, observed_at=received_at))
        return articles

