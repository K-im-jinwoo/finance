from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from stock_assistant.cli import main
from stock_assistant.client import StockClient
from stock_assistant.http_api import StockApi
from stock_assistant.models import AssetType, CompanyKind, Market, Security
from stock_assistant.news_discovery import NewsDiscoveryStore, discover_news, latest_news_discovery
from stock_assistant.news_cost import NewsCostBlocked
from stock_assistant.pipeline import CandidatePipeline
from stock_assistant.presentation import render_candidate_report
from stock_assistant.providers.http import AuthenticationError, JsonResponse, RateLimitError, UpstreamSchemaError
from stock_assistant.providers.news import NaverNewsClient, NewsArticle, normalize_naver_news
from stock_assistant.reports import build_candidate_report, report_to_dict
from stock_assistant.repository import StockRepository
from test_screening import good_financial, make_bars


UTC = timezone.utc
NOW = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)


def security(symbol="900099", name="참빛테크", **kwargs):
    return Security(symbol, name, Market.KOSDAQ, kwargs.get("asset_type", AssetType.COMMON),
                    kwargs.get("company_kind", CompanyKind.GENERAL), date(2000, 1, 1),
                    kwargs.get("delisted_on"))


def article(title="참빛테크, 반도체 공급계약 체결", **kwargs):
    return NewsArticle(title, kwargs.get("url", "https://publisher.example/news/1"),
                       kwargs.get("published_at", NOW - timedelta(hours=1)),
                       kwargs.get("observed_at", NOW), kwargs.get("description", ""))


def raw_item(**kwargs):
    return {"title": "<b>참빛테크</b>, 반도체 공급계약 체결", "originallink": "https://publisher.example/news/1?utm_source=x",
            "description": "<b>공급계약</b> 보도", "pubDate": "Thu, 01 Oct 2026 09:30:00 +0900", **kwargs}


class NewsProviderTests(unittest.TestCase):
    def test_official_api_shape_headers_limits_and_receipt_time(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            return JsonResponse(200, {"items": [raw_item()]})
        client = NaverNewsClient("fixture-id", "fixture-secret", fetch_json=fetch,
                                 clock=lambda: NOW + timedelta(seconds=2), budget=Mock())
        items = client.collect(observed_at=NOW, queries=("공급계약",), display=50)
        self.assertEqual(calls[0][0], "https://naverapihub.apigw.ntruss.com/search/v1/news")
        self.assertEqual(calls[0][1]["query"], {"query": "공급계약", "display": "50", "start": "1", "sort": "date", "format": "json"})
        self.assertEqual(calls[0][1]["headers"], {"X-NCP-APIGW-API-KEY-ID": "fixture-id", "X-NCP-APIGW-API-KEY": "fixture-secret"})
        self.assertEqual(calls[0][1]["timeout_seconds"], 10)
        self.assertEqual(items[0].observed_at, NOW + timedelta(seconds=2))
        self.assertEqual(items[0].published_at, NOW - timedelta(minutes=30))
        self.assertEqual(items[0].title, "참빛테크, 반도체 공급계약 체결")
        self.assertEqual(items[0].url, "https://publisher.example/news/1")
        self.assertEqual(items[0].timestamp_basis, "PROVIDER_PUBDATE")

    def test_empty_is_empty_and_invalid_shapes_urls_dates_fail_closed(self):
        self.assertEqual(normalize_naver_news({"items": []}, observed_at=NOW), [])
        for payload in ({}, {"items": {}}, {"items": [raw_item(pubDate="bad")]},
                        {"items": [raw_item(originallink="javascript:alert(1)")]},
                        {"items": [raw_item(originallink="https://user:password@example.com/news")]},
                        {"items": [raw_item(pubDate="Thu, 01 Oct 2026 09:30:00")]},
                        {"items": [raw_item()] * 101}):
            with self.subTest(payload=str(payload)[:50]), self.assertRaises(UpstreamSchemaError):
                normalize_naver_news(payload, observed_at=NOW)

    def test_limits_and_credentials_are_checked_before_network(self):
        with self.assertRaises(AuthenticationError):
            NaverNewsClient("", "fixture")
        with self.assertRaises(AuthenticationError):
            NaverNewsClient("fixture\nheader", "fixture")
        client = NaverNewsClient("fixture", "fixture", fetch_json=lambda *a, **k: self.fail("network called"))
        for kwargs in ({"display": 101}, {"display": True}, {"queries": ()}, {"queries": ("a", "a")},
                       {"queries": ("a", "b", "c", "d", "e", "f")}, {"queries": ("x" * 81,)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.collect(observed_at=NOW, **kwargs)

    def test_provider_errors_are_not_retried_or_turned_into_success(self):
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise RateLimitError("provider returned HTTP 429")
        client = NaverNewsClient("fixture", "fixture", fetch_json=fail, budget=Mock())
        with self.assertRaises(RateLimitError):
            client.collect(observed_at=NOW)
        self.assertEqual(len(calls), 1)


class NewsMatchingTests(unittest.TestCase):
    def run_discovery(self, items, universe=None):
        return discover_news(items, universe or [security()], as_of=NOW)

    def test_contract_earnings_approval_policy_are_unverified_research_leads(self):
        for title, category in (("참빛테크, 공급계약 체결", "CONTRACT"),
                                ("참빛테크 영업이익 증가, 사상 최대 실적", "EARNINGS"),
                                ("참빛테크, 적자 끝 흑자전환", "EARNINGS"),
                                ("참빛테크는 신약 허가 획득", "APPROVAL"),
                                ("참빛테크, 정부 정책 수혜기업 선정", "POLICY")):
            with self.subTest(title=title):
                events = self.run_discovery([article(title)])["events"]
                self.assertEqual(events[0]["category"], category)
                self.assertFalse(events[0]["official"])
                self.assertEqual(events[0]["symbol"], "900099")

    def test_name_boundaries_multi_issuer_and_unmapped_themes_are_not_guessed(self):
        universe = [security(), security("900098", "다른테크"), security("034730", "SK"),
                    security("000660", "SK하이닉스")]
        self.assertEqual(self.run_discovery([article("참빛테크놀로지, 공급계약 체결")], universe)["events"], [])
        self.assertEqual(self.run_discovery([article("참빛테크·다른테크 공급계약 체결")], universe)["filter_counts"]["ambiguous"], 1)
        self.assertEqual(self.run_discovery([article("정부 반도체 정책 수혜 확정")], universe)["events"], [])
        event = self.run_discovery([article("SK하이닉스, 공급계약 체결")], universe)["events"][0]
        self.assertEqual(event["symbol"], "000660")

    def test_stale_future_observation_and_lookback_boundary(self):
        items = [article(published_at=NOW - timedelta(hours=49)),
                 article(url="https://example.com/future", published_at=NOW + timedelta(seconds=1)),
                 article(url="https://example.com/unobserved", observed_at=NOW + timedelta(seconds=1)),
                 article("참빛테크, 신규 공급계약 체결", url="https://example.com/boundary", published_at=NOW - timedelta(hours=48))]
        run = self.run_discovery(items)
        self.assertEqual(len(run["events"]), 1)
        self.assertEqual(run["filter_counts"]["stale"], 1)
        self.assertEqual(run["filter_counts"]["future"], 2)

    def test_unicode_subsidiary_shorthand_does_not_match_parent(self):
        universe = [security("267250", "HD현대"), security("329180", "HD현대중공업")]
        for title in ("HD현대重, 해상변전소 수주", "HD현대É, 공급계약 체결", "重HD현대, 공급계약 체결"):
            with self.subTest(title=title):
                self.assertEqual(self.run_discovery([article(title)], universe)["events"], [])
        for title, symbol in (("HD현대중공업, 해상변전소 수주", "329180"),
                              ("HD현대는 정부 정책 수혜기업 선정", "267250"),
                              ("HD현대에서 공급계약 체결", "267250")):
            with self.subTest(title=title):
                self.assertEqual(self.run_discovery([article(title)], universe)["events"][0]["symbol"], symbol)

    def test_noise_expectations_and_unsupported_listings(self):
        for title in ("참빛테크 수주 기대감", "참빛테크 공급계약 추진 검토", "참빛테크 신약 승인 신청",
                      "참빛테크 영업이익 감소", "[포토] 참빛테크 공급계약 행사"):
            with self.subTest(title=title):
                self.assertEqual(self.run_discovery([article(title)])["events"], [])
        for s in (security(asset_type=AssetType.SPAC), security(delisted_on=date(2026, 9, 30))):
            self.assertEqual(self.run_discovery([article()], [s])["events"], [])

    def test_query_overlaps_and_reprinted_titles_do_not_multiply_evidence(self):
        items = [article(), article(), article(url="https://another.example/news/2")]
        run = self.run_discovery(items)
        self.assertEqual(len(run["events"]), 1)
        self.assertEqual(run["filter_counts"]["duplicate"], 2)


class NewsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "fixture.sqlite3"
        self.store = NewsDiscoveryStore(self.database)

    def save(self, items=None, at=NOW):
        run = discover_news(items if items is not None else [article()], [security()], as_of=at)
        self.store.save(run)
        return run

    def test_idempotence_first_observation_and_point_in_time_reads(self):
        run = self.save()
        self.store.save(run)
        self.save([replace(article(), observed_at=NOW + timedelta(minutes=5))], NOW + timedelta(minutes=5))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM news_discovery_events").fetchone()[0], 1)
        current = latest_news_discovery(self.database, as_of=NOW + timedelta(minutes=5))
        self.assertEqual(current["candidates"][0]["evidence"][0]["first_seen_at"], NOW.isoformat())
        self.assertEqual(latest_news_discovery(self.database, as_of=NOW - timedelta(seconds=1))["candidates"], [])

    def test_refresh_failure_and_stale_refresh_do_not_reuse_old_success(self):
        self.save()
        self.assertEqual(latest_news_discovery(self.database, as_of=NOW + timedelta(hours=13))["reason"], "NEWS_REFRESH_STALE")
        self.store.save({"observed_at": (NOW + timedelta(minutes=5)).isoformat(), "status": "UNAVAILABLE",
                         "error_code": "RATE_LIMITED", "events": []})
        current = latest_news_discovery(self.database, as_of=NOW + timedelta(minutes=5))
        self.assertEqual(current["status"], "UNAVAILABLE")
        self.assertEqual(current["reason"], "RATE_LIMITED")
        self.assertEqual(current["candidates"], [])

    def test_article_expiry_is_not_extended_by_refresh(self):
        self.save([article(published_at=NOW - timedelta(hours=47))])
        self.save([], NOW + timedelta(hours=2))
        self.assertEqual(latest_news_discovery(self.database, as_of=NOW + timedelta(hours=2))["candidates"], [])

    def test_contract_cancellation_blocks_the_previous_positive_even_after_an_empty_refresh(self):
        self.save()
        at = NOW + timedelta(minutes=5)
        cancellation = article("참빛테크, 공급계약 취소", url="https://example.com/cancel",
                               published_at=at - timedelta(seconds=1), observed_at=at)
        self.save([cancellation], at)
        self.save([], at + timedelta(minutes=1))
        self.assertEqual(latest_news_discovery(self.database, as_of=at + timedelta(minutes=1))["candidates"], [])

    def test_legacy_read_does_not_migrate_or_change_schema(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        repo = StockRepository(path)
        with closing(sqlite3.connect(path)) as connection:
            before = connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
        self.assertEqual(latest_news_discovery(path, as_of=NOW)["reason"], "NEWS_NOT_COLLECTED")
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(before, connection.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall())

    def test_stored_legacy_alias_match_is_filtered_without_deleting_evidence(self):
        universe = [security("267250", "HD현대"), security("329180", "HD현대중공업")]
        run = discover_news([
            article("HD현대, 해상변전소 수주"),
            article("HD현대중공업, 공급계약 체결", url="https://example.com/child"),
        ], universe, as_of=NOW)
        parent = next(event for event in run["events"] if event["symbol"] == "267250")
        parent["title"] = "HD현대重, 해상변전소 수주"
        self.store.save(run)
        current = latest_news_discovery(self.database, as_of=NOW)
        self.assertEqual([c["symbol"] for c in current["candidates"]], ["329180"])
        self.assertEqual(current["matched_symbols"], 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM news_discovery_events").fetchone()[0], 2)


class NewsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "fixture.sqlite3"
        self.repo = StockRepository(self.database)
        stocks = [security(f"90000{i}", f"기존기업{i}") for i in range(1, 6)] + [security()]
        self.repo.save_securities(stocks)
        for s in stocks:
            self.repo.save_bars([replace(b, symbol=s.symbol) for b in make_bars()])
            if s.symbol != "900099":
                self.repo.save_financial_snapshot(replace(good_financial(), symbol=s.symbol))
        self.store = NewsDiscoveryStore(self.database)
        self.store.save(discover_news([article()], stocks, as_of=NOW))
        self.pipeline = CandidatePipeline(self.repo)

    def test_low_score_news_lead_appears_and_enters_enrichment_without_replacing_top_five(self):
        base = report_to_dict(self.pipeline.run(as_of=NOW).report)
        report = report_to_dict(self.pipeline.run(as_of=NOW, include_news=True).report)
        self.assertEqual(report["candidates"], base["candidates"])
        self.assertNotIn("900099", {c["symbol"] for c in report["candidates"]})
        self.assertEqual(report["news_discovery"]["candidates"][0]["symbol"], "900099")
        self.assertEqual(report["news_discovery"]["candidates"][0]["status"], "REVIEW_REQUIRED")
        self.assertIn("900099", self.pipeline.enrichment_symbols(as_of=NOW, limit=5, include_news=True))
        self.assertNotIn("900099", self.pipeline.enrichment_symbols(as_of=NOW, limit=5))
        self.assertEqual(report["contract_version"], "1.3")

    def test_repeated_news_is_labelled_unchanged_and_report_hash_preserves_evidence(self):
        first = report_to_dict(self.pipeline.run(as_of=NOW, include_news=True).report)
        second = report_to_dict(self.pipeline.run(as_of=NOW + timedelta(minutes=5), include_news=True).report)
        self.assertEqual(first["news_discovery"]["candidates"][0]["change"], "NEW")
        self.assertEqual(second["news_discovery"]["candidates"][0]["change"], "UNCHANGED")
        self.assertNotEqual(build_candidate_report(NOW, [], news_discovery={"status": "OK"}).report_id,
                            build_candidate_report(NOW, [], news_discovery={"status": "UNAVAILABLE"}).report_id)
        legacy = report_to_dict(build_candidate_report(NOW, []))
        self.assertEqual(legacy["contract_version"], "1.2")
        self.assertNotIn("news_discovery", legacy)

    def test_renderer_exposes_evidence_and_failure_is_distinct_from_no_news(self):
        report = report_to_dict(self.pipeline.run(as_of=NOW, include_news=True).report)
        chunks = render_candidate_report(report)
        rendered = "\n".join(chunks)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        for text in ("뉴스 발견 — 추가 검토", "참빛테크 (900099)", "추가 검토 / 매수 보류",
                     "https://publisher.example/news/1", "공급자 기사시각", "최초 발견시각"):
            self.assertIn(text, rendered)
        failed = report_to_dict(self.pipeline.run(as_of=NOW, include_news=True, news_fetch_failed=True).report)
        self.assertEqual(failed["news_discovery"]["candidates"], [])
        self.assertIn("뉴스 수집: 확인 불가", "\n".join(render_candidate_report(failed)))

    def test_cli_collects_with_fixture_credentials_and_reports_safe_provider_failure(self):
        root = Path(self.temp.name)
        (root / "id").write_text("fixture-id", encoding="utf-8")
        (root / "secret").write_text("fixture-secret", encoding="utf-8")
        args = ["discover-news", "--database", str(self.database), "--client-id-file", str(root / "id"),
                "--client-secret-file", str(root / "secret")]
        with patch("stock_assistant.cli.datetime") as clock, patch("stock_assistant.cli.NaverNewsClient") as client, patch("sys.stdout", new_callable=StringIO) as output:
            clock.now.return_value = NOW
            client.return_value.collect.return_value = [article()]
            self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(output.getvalue())["matched_events"], 1)
        with patch("stock_assistant.cli.datetime") as clock, patch("stock_assistant.cli.NaverNewsClient") as client, patch("sys.stderr", new_callable=StringIO) as output:
            clock.now.return_value = NOW + timedelta(minutes=1)
            client.return_value.collect.side_effect = RateLimitError("provider returned HTTP 429")
            self.assertEqual(main(args), 1)
            self.assertEqual(json.loads(output.getvalue())["error_code"], "RATE_LIMITED")
            self.assertNotIn("fixture-secret", output.getvalue())
        self.assertEqual(latest_news_discovery(self.database, as_of=NOW + timedelta(minutes=1))["status"], "UNAVAILABLE")

    def test_cost_guard_skips_cooldown_but_marks_limit_block_unavailable(self):
        root = Path(self.temp.name)
        (root / "id").write_text("fixture-id", encoding="utf-8")
        (root / "secret").write_text("fixture-secret", encoding="utf-8")
        args = ["discover-news", "--database", str(self.database), "--client-id-file", str(root / "id"),
                "--client-secret-file", str(root / "secret")]
        for code, expected in (("NEWS_REFRESH_COOLDOWN", 0), ("NEWS_COST_LIMIT_REACHED", 1)):
            with self.subTest(code=code), patch("stock_assistant.cli.datetime") as clock, \
                 patch("stock_assistant.cli.NaverNewsClient") as client, \
                 patch("sys.stdout", new_callable=StringIO), patch("sys.stderr", new_callable=StringIO):
                clock.now.return_value = NOW
                client.return_value.collect.side_effect = NewsCostBlocked(code)
                self.assertEqual(main(args), expected)
            self.assertEqual(latest_news_discovery(self.database, as_of=NOW)["status"],
                             "OK" if expected == 0 else "UNAVAILABLE")

    def test_api_opt_in_and_client_validate_boolean_without_changing_legacy_payload(self):
        api = StockApi(self.repo, shared_secret="fixture")
        payload = {"as_of": NOW.isoformat(), "include_news": True}
        response = api.dispatch("POST", "/v1/repository/candidates",
                                headers={"Authorization": "Bearer fixture"},
                                body=json.dumps(payload).encode())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["report"]["news_discovery"]["candidates"][0]["symbol"], "900099")
        payload["include_news"] = "true"
        bad = api.dispatch("POST", "/v1/repository/candidates", headers={"Authorization": "Bearer fixture"},
                           body=json.dumps(payload).encode())
        self.assertEqual(bad.status, 422)
        client = StockClient("http://127.0.0.1:9120", Path("unused-fixture-token"))
        with patch.object(StockClient, "_request", return_value={}) as request:
            client.generate_candidates(as_of=NOW)
            self.assertNotIn("include_news", request.call_args.args[2])
            client.generate_candidates(as_of=NOW, include_news=True)
            self.assertTrue(request.call_args.args[2]["include_news"])
            with self.assertRaises(ValueError):
                client.generate_candidates(as_of=NOW, include_news="true")

    def test_a_news_lead_cannot_override_a_financial_exclusion(self):
        self.repo.save_financial_snapshot(replace(good_financial(), symbol="900099", operating_income=-1,
                                                   annual_operating_income=-1, ttm_operating_income=-1))
        report = report_to_dict(self.pipeline.run(as_of=NOW, include_news=True).report)
        self.assertEqual(report["news_discovery"]["candidates"][0]["status"], "EXCLUDED")
        rendered = "\n".join(render_candidate_report(report))
        self.assertIn("기존 규칙상 제외", rendered)
        self.assertIn("OPERATING_LOSS", rendered)


if __name__ == "__main__":
    unittest.main()
