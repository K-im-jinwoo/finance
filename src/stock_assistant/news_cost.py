"""Fail closed before billable news requests; reserve attempted calls durably."""
from __future__ import annotations

import os
import sqlite3
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

from .providers.http import ProviderError

DAILY_LIMIT = 20
MONTHLY_LIMIT = 500
COOLDOWN = timedelta(hours=1)
KST = timezone(timedelta(hours=9))
POLICY_URLS = ("https://guide.ncloud-docs.com/docs/apihub-overview",
               "https://guide.ncloud-docs.com/docs/apihub-spec")
FREE_STATEMENT = "NAVER API HUB 서비스는 한시적으로 무료로 제공되며, 유료 전환 시 사전에 별도 공지로 안내될 예정입니다."


class NewsCostBlocked(ProviderError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def _policy_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "stock-assistant-cost-guard/1.0",
                                                   "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=8) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
        if response.status != 200 or response.geturl() != url or len(raw) > 8 * 1024 * 1024:
            raise ValueError("policy response invalid")
    return raw.decode("utf-8")


def require_free_policy(fetch_text=None):
    """Check both official public guides on every batch, without an API key."""
    fetch = fetch_text or _policy_text
    try:
        for url in POLICY_URLS:
            parser = _VisibleText()
            parser.feed(fetch(url))
            text = "".join("".join(parser.parts).split())
            if "".join(FREE_STATEMENT.split()) not in text:
                raise ValueError("free statement changed")
    except Exception:
        raise NewsCostBlocked("NEWS_FREE_POLICY_UNCONFIRMED") from None


class NewsApiBudget:
    def __init__(self, path=None, *, free_check=None):
        self.path = Path(path or os.getenv("STOCK_NEWS_USAGE_FILE", "/var/lib/stock/news-api-usage.sqlite3"))
        self.free_check = free_check or require_free_policy

    @staticmethod
    def initialize(path: Path, *, at: datetime, used_today: int, used_month: int):
        """Explicit setup only. Runtime never silently recreates a lost usage ledger."""
        if at.tzinfo is None or any(type(n) is not int or n < 0 for n in (used_today, used_month)) or used_month < used_today:
            raise ValueError("invalid initial usage")
        if path.exists() or path.is_symlink():
            raise ValueError("existing news usage ledger must be preserved")
        local_day = at.astimezone(KST).date().isoformat()
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE news_api_budget (id INTEGER PRIMARY KEY CHECK(id=1), "
                               "day TEXT NOT NULL, month TEXT NOT NULL, daily_used INTEGER NOT NULL, "
                               "monthly_used INTEGER NOT NULL, last_reserved_at TEXT NOT NULL, "
                               "halted_month TEXT NOT NULL)")
            connection.execute("INSERT INTO news_api_budget VALUES(1,?,?,?,?,?,?)",
                               (local_day, local_day[:7], used_today, used_month,
                                at.astimezone(timezone.utc).isoformat(), ""))

    def reserve(self, calls: int, *, at: datetime, cooldown=True):
        if at.tzinfo is None or type(calls) is not int or not 1 <= calls <= 5:
            raise ValueError("invalid news call reservation")
        if self.path.is_symlink() or not self.path.is_file():
            raise NewsCostBlocked("NEWS_USAGE_STATE_UNAVAILABLE")
        now = at.astimezone(timezone.utc)
        day = at.astimezone(KST).date().isoformat()
        month = day[:7]
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute("SELECT day,month,daily_used,monthly_used,last_reserved_at,halted_month "
                                          "FROM news_api_budget WHERE id=1").fetchall()
                if len(rows) != 1:
                    raise ValueError("missing budget state")
                old_day, old_month, daily, monthly, last, halted = rows[0]
                old_date = date.fromisoformat(old_day)
                last_at = datetime.fromisoformat(last)
                if (old_day != old_date.isoformat() or old_month != old_day[:7] or last_at.tzinfo is None
                        or any(type(n) is not int or n < 0 for n in (daily, monthly)) or monthly < daily
                        or halted not in ("", old_month)):
                    raise ValueError("invalid budget state")
                if day < old_day or now < last_at:
                    raise NewsCostBlocked("NEWS_USAGE_CLOCK_REVERSED")
                if halted == month:
                    raise NewsCostBlocked("NEWS_PROVIDER_QUOTA_BLOCKED")
                daily = daily if old_day == day else 0
                monthly = monthly if old_month == month else 0
                if daily + calls > DAILY_LIMIT or monthly + calls > MONTHLY_LIMIT:
                    raise NewsCostBlocked("NEWS_COST_LIMIT_REACHED")
                if cooldown and now - last_at < COOLDOWN:
                    raise NewsCostBlocked("NEWS_REFRESH_COOLDOWN")
                self.free_check()
                # Commit the entire batch before the first request. Never refund on failure.
                connection.execute("UPDATE news_api_budget SET day=?,month=?,daily_used=?,monthly_used=?,"
                                   "last_reserved_at=?,halted_month='' WHERE id=1",
                                   (day, month, daily + calls, monthly + calls, now.isoformat()))
        except NewsCostBlocked:
            raise
        except Exception:
            raise NewsCostBlocked("NEWS_USAGE_STATE_UNAVAILABLE") from None

    def halt_provider(self, *, at: datetime):
        """A provider quota/auth rejection blocks further attempts for this KST month."""
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=2)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE news_api_budget SET halted_month=month WHERE id=1 AND month=?",
                                   (at.astimezone(KST).strftime("%Y-%m"),))
        except Exception:
            raise NewsCostBlocked("NEWS_USAGE_STATE_UNAVAILABLE") from None
