"""Opt-in read-only audits and isolated financial proposal state.

Usage: python -m stock_assistant.research_cli COMMAND --database PATH
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys

from .financial_research import AnnualProfit, POLICY_STATUS, POLICY_VERSION, evaluate_financial
from .financial_research_state import FinancialResearchState
from .models import to_json_value
from .research_repository import ReadOnlyResearchRepository
from .screening_diagnostics import audit_screening
from .validation import parse_decimal, validate_analysis_time
from .dart_research import DartResearchCollector
from .research_data import ResearchData
from .research_workflow import collect_calendar, collect_targeted_news, market_session, import_reviews, review_queue
from .unified_research import build_recommendations, render_recommendations
from .providers.http import ProviderError


def _histories(path: Path | None) -> dict[str, tuple[AnnualProfit, ...]]:
    if path is None:
        return {}
    if path.stat().st_size > 2_000_000:
        raise ValueError("annual history file exceeds 2 MB")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) > 20_000:
        raise ValueError("annual history must be a bounded array")
    grouped = {}
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("annual history entries must be objects")
        point = AnnualProfit(
            item["symbol"], date.fromisoformat(item["period_end"]), parse_decimal(item["operating_income"], "annual operating_income"),
            datetime.fromisoformat(item["published_at"]), datetime.fromisoformat(item["observed_at"]),
            item["receipt_id"], item["statement_basis"], item["source_url"],
        )
        grouped.setdefault(point.symbol, []).append(point)
    return {key: tuple(values) for key, values in grouped.items()}


def review_universe(repository, *, as_of: datetime, annual_histories: dict | None = None) -> list:
    histories = annual_histories or {}
    reviews = []
    for security in repository.list_securities():
        try:
            reviews.append(evaluate_financial(security, repository.latest_financial(security.symbol, as_of=as_of),
                                             as_of=as_of, annual_history=histories.get(security.symbol, ())))
        except ValueError as exc:
            raise ValueError(f"invalid financial input for {security.symbol}: {exc}") from exc
    return reviews


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("audit", "financials", "collect-dart", "collect-news", "collect-archive", "collect-calendar",
                 "import-reviews", "review-queue", "recommend", "refresh-unified", "refresh-swing", "evaluate-swing"):
        command = commands.add_parser(name)
        command.add_argument("--database", type=Path, required=True)
        if name in {"audit", "financials", "review-queue", "recommend", "evaluate-swing"}:
            command.add_argument("--as-of", type=datetime.fromisoformat)
        if name not in {"audit", "financials"}:
            command.add_argument("--research-database", type=Path, required=True)
    commands.choices["audit"].add_argument("--reports", type=int, default=8)
    commands.choices["financials"].add_argument("--annual-history", type=Path)
    commands.choices["financials"].add_argument("--state-database", type=Path,
                                             help="optional separate local research state; source DB is never writable")
    dart = commands.choices["collect-dart"]
    dart.add_argument("--api-key-file", type=Path, required=True)
    dart.add_argument("--batch-size", type=int, default=25)
    dart.add_argument("--symbols", help="optional comma-separated symbols; default rotates the entire ordinary-stock universe")
    dart.add_argument("--force", action="store_true")
    dart.add_argument("--request-limit", type=int, default=2000)
    for name in ("collect-news", "collect-archive", "refresh-unified", "refresh-swing"):
        commands.choices[name].add_argument("--client-id-file", type=Path, required=True)
        commands.choices[name].add_argument("--client-secret-file", type=Path, required=True)
        commands.choices[name].add_argument("--usage-database", type=Path, required=True)
    commands.choices["collect-calendar"].add_argument("--token-file", type=Path, required=True,
                                                   help="already issued token; this command never issues OAuth tokens")
    commands.choices["collect-archive"].add_argument("--pages", type=int, default=5)
    commands.choices["collect-archive"].add_argument("--symbols")
    refresh = commands.choices["refresh-unified"]
    refresh.add_argument("--api-key-file", type=Path, required=True)
    refresh.add_argument("--toss-client-id-file", type=Path, required=True)
    refresh.add_argument("--toss-client-secret-file", type=Path, required=True)
    refresh.add_argument("--batch-size", type=int, default=25)
    refresh.add_argument("--archive-pages", type=int, help="default: 25 for actionable research, 5 otherwise")
    refresh.add_argument("--symbols")
    refresh.add_argument("--actionable", action="store_true")
    refresh.add_argument("--format", choices=("json", "text"), default="text")
    swing = commands.choices["refresh-swing"]
    for option in ("--krx-auth-key-file", "--toss-client-id-file", "--toss-client-secret-file"):
        swing.add_argument(option, type=Path, required=True)
    commands.choices["collect-news"].add_argument("--limit", type=int, default=5)
    commands.choices["import-reviews"].add_argument("--review-file", type=Path, required=True)
    commands.choices["recommend"].add_argument("--format", choices=("json", "text"), default="json")
    commands.choices["recommend"].add_argument("--actionable", action="store_true")
    commands.choices["recommend"].add_argument("--save-report", action="store_true",
                                            help="save this new-contract report only to the separate research data database")
    commands.choices["review-queue"].add_argument("--limit", type=int, default=25)
    args = parser.parse_args(argv)
    try:
        as_of = validate_analysis_time(getattr(args, "as_of", None) or datetime.now(timezone.utc))
        repository = ReadOnlyResearchRepository(args.database)
        if args.command == "audit":
            result = audit_screening(repository, as_of=as_of, report_limit=args.reports)
        elif args.command == "financials":
            reviews = review_universe(repository, as_of=as_of, annual_histories=_histories(args.annual_history))
            visible_reviews = []
            for review in reviews:
                item = to_json_value(review)
                item.pop("source_inputs")
                visible_reviews.append(item)
            result = {"policy_version": POLICY_VERSION, "policy_status": POLICY_STATUS,
                      "as_of": as_of.isoformat(), "source_read_only": True, "orders_enabled": False,
                      "paper_account_eligible": False, "gate_counts": dict(Counter(review.gate for review in reviews)),
                      "reviews": visible_reviews,
                      "next_news_review_symbols": [review.symbol for review in sorted(
                          reviews, key=lambda review: (-(review.financial_score or Decimal("0")), review.symbol))
                          if review.gate == "PASS"],
                      "financial_fetch_symbols": [review.symbol for review in reviews if review.gate == "VERIFY_PENDING"
                          and any(reason in {"FINANCIAL_SNAPSHOT_UNAVAILABLE", "PROFIT_PERIODS_UNAVAILABLE", "OCF_UNAVAILABLE", "TTM_STALE"}
                                  for reason in review.blocking_reasons)],
                      "classification_review_symbols": [review.symbol for review in reviews
                          if "COMPANY_KIND_UNAVAILABLE" in review.blocking_reasons]}
            if args.state_database:
                result["state"] = FinancialResearchState(args.state_database, source_database=args.database).sync(reviews, as_of=as_of)
        elif args.command == "evaluate-swing":
            from .performance import evaluate_swing_performance
            from .repository import StockRepository
            result = evaluate_swing_performance(StockRepository(repository.database_path), evaluated_at=as_of,
                                                research_database=args.research_database)
        else:
            store = ResearchData(args.research_database, source_database=args.database)
            if args.command == "collect-dart":
                from .providers.dart import DartClient
                symbols = args.symbols.split(",") if args.symbols else None
                result = DartResearchCollector(repository, store, DartClient(_credential(args.api_key_file)),
                                               request_limit=args.request_limit).run(
                    symbols=symbols, batch_size=args.batch_size, force=args.force)
            elif args.command in {"collect-news", "collect-archive", "refresh-unified", "refresh-swing"}:
                from .news_cost import NewsApiBudget
                from .providers.news import NaverNewsClient
                protected = (repository.database_path, store.path)
                if any(args.usage_database.resolve() == p or (args.usage_database.exists() and args.usage_database.samefile(p))
                       for p in protected):
                    raise ValueError("news usage database must be separate from source and research data")
                client = NaverNewsClient(_credential(args.client_id_file), _credential(args.client_secret_file),
                                         budget=NewsApiBudget(args.usage_database))
                if args.command == "collect-news":
                    result = collect_targeted_news(repository, store, client, limit=args.limit)
                elif args.command == "collect-archive":
                    from .news_archive import collect_archive
                    result = collect_archive(repository, store, client, pages=args.pages, symbols=args.symbols.split(',') if args.symbols else None)
                elif args.command == "refresh-swing":
                    from .providers.http import get_json
                    from .providers.krx import KrxClient
                    from .swing_theme import refresh_swing
                    from .unified_cycle import shared_calendar
                    market = shared_calendar(_credential(args.toss_client_id_file),
                                             _credential(args.toss_client_secret_file), fetch_json=get_json)
                    result = refresh_swing(repository, store, krx_client=KrxClient(_credential(args.krx_auth_key_file)),
                                           market_client=market, news_client=client)
                else:
                    from .unified_cycle import refresh_unified, shared_calendar
                    from .providers.dart import DartClient
                    key = _credential(args.api_key_file)
                    result = refresh_unified(repository, store, dart_client=DartClient(key), news_client=client,
                        calendar_client=shared_calendar(_credential(args.toss_client_id_file), _credential(args.toss_client_secret_file)),
                        dart_key=key, batch_size=args.batch_size, archive_pages=args.archive_pages,
                        seed_symbols=args.symbols.split(',') if args.symbols else None,
                        actionable=args.actionable)
                    if args.format == 'text':
                        print(render_recommendations(result))
                        return 0
            elif args.command == "collect-calendar":
                from .research_calendar import ExistingTokenCalendarClient
                client = ExistingTokenCalendarClient(_credential(args.token_file))
                result = collect_calendar(store, client)
            elif args.command == "import-reviews":
                if args.review_file.stat().st_size > 5_000_000:
                    raise ValueError("review file exceeds 5 MB")
                result = import_reviews(store, json.loads(args.review_file.read_text(encoding="utf-8")), as_of=as_of)
            elif args.command == "review-queue":
                result = review_queue(repository, store, as_of=as_of, limit=args.limit)
            else:
                session, closed = market_session(store, as_of=as_of)
                fallback = None
                if args.actionable:
                    from .unified_cycle import _latest_available_price_session
                    fallback = _latest_available_price_session(repository, as_of=as_of)
                result = build_recommendations(repository, store, as_of=as_of, expected_session=session, verified_close_at=closed,
                                               actionable=args.actionable, fallback_price_session=fallback)
                if args.save_report:
                    store.put("unified_report", "ALL", result, observed_at=datetime.now(timezone.utc))
                if args.format == "text":
                    print(render_recommendations(result))
                    return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "collect-dart" and any(r["status"] == "FAILED" for r in result["results"]) else 0
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, ProviderError) as exc:
        # Provider exceptions can carry URLs or request metadata; emit only their safe code.
        error = exc.code if isinstance(exc, ProviderError) else str(exc)
        print(json.dumps({"status": "FAILED", "error_class": type(exc).__name__, "error": error},
                         ensure_ascii=False), file=sys.stderr)
        return 1


def _credential(path):
    if path.stat().st_size > 4096:
        raise ValueError("credential file is too large")
    value = path.read_text(encoding="utf-8").strip()
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError("credential file is empty or invalid")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
