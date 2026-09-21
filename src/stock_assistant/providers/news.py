from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Evidence
from .http import UpstreamSchemaError


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

