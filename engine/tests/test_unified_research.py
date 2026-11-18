from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock

from stock_assistant.dart_research import DartResearchCollector
from stock_assistant.financial_research import evaluate_financial
from stock_assistant.models import Evidence, to_json_value
from stock_assistant.news_research import ReviewedEvent, score_news
from stock_assistant.repository import StockRepository
from stock_assistant.research_cli import main
from stock_assistant.research_data import ResearchData
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.research_workflow import collect_targeted_news, import_reviews as real_import_reviews, review_queue, market_session
from stock_assistant.research_calendar import ExistingTokenCalendarClient
from stock_assistant.providers.http import JsonResponse
from stock_assistant.unified_research import build_recommendations, evaluate_unified, price_review, render_recommendations
from tests.helpers import make_bars
from tests.test_dart_research import FakeDart, url
from tests.test_financial_research import history
from tests.test_screening import AS_OF, good_financial, samsung


def import_reviews(store, payload, *, as_of):
    return real_import_reviews(store, payload, as_of=as_of, clock=lambda: as_of)


def coverage():
    return {"symbol": "005930", "start": "2023-09-19", "end": "2026-09-19", "observed_at": AS_OF.isoformat(),
            "reviewed_at": AS_OF.isoformat(), "status": "REVIEWED", "sources": ["NEWS_ARCHIVE", "DART_HISTORY"],
            "source_refs": ["archive", "dart"], "reviewer": "fixture-reviewer"}


def event(key="CONTRACT-1", **kw):
    source = Evidence("DART", "계약 체결", url("20260901000001"), AS_OF - timedelta(days=19), AS_OF, True, ("ISSUER:005930",))
    return replace(ReviewedEvent("005930", key, "CONTRACT", "POSITIVE", "ACTIVE", "HIGH", True, False,
                                 AS_OF - timedelta(days=19), AS_OF, AS_OF + timedelta(days=30),
                                 "fixture-reviewer", "원문에서 규모와 유효 기간 확인", (source,)), **kw)


class NewsResearchTests(unittest.TestCase):
    def score(self, events=(), cover=None, **kw):
        return score_news("005930", events, coverage() if cover is None else cover, as_of=AS_OF, **kw)

    def test_completed_neutral_search_is_fifty_missing_is_not_neutral(self):
        self.assertEqual(self.score()["news_score"], 50)
        self.assertIsNone(score_news("005930", [], None, as_of=AS_OF)["news_score"])
        self.assertIsNone(self.score(cover={**coverage(), "status": "BOUNDED"})["news_score"])

    def test_duplicate_articles_one_event_and_financial_earnings_not_double_counted(self):
        confirmed = event()
        self.assertEqual(self.score([confirmed, confirmed])["news_score"], 70)
        self.assertEqual(self.score([event(category="EARNINGS")])["news_score"], 50)
        with self.assertRaisesRegex(ValueError, "same event_id"):
            self.score([event("contract-a"), event("contract-b")])

    def test_latest_cancellation_removes_positive_score_without_erasing_original(self):
        prior = replace(event(), reviewed_at=AS_OF - timedelta(days=1), evidence=(replace(event().evidence[0], observed_at=AS_OF - timedelta(days=1)),))
        cancelled = replace(event(), state="CANCELLED")
        result = self.score([cancelled, prior])
        self.assertEqual(result["news_score"], 50)
        self.assertEqual(result["events"][0]["status"], "CANCELLED")

    def test_expired_positive_zero_and_unresolved_older_negative_retained(self):
        self.assertEqual(self.score([event(valid_until=AS_OF - timedelta(days=1))])["news_score"], 50)
        old = replace(event(), direction="NEGATIVE", category="MANAGEMENT", announced_at=AS_OF - timedelta(days=365 * 12),
                      valid_until=None, hard_exclusion=True)
        self.assertTrue(self.score([old])["hard_exclusions"])

    def test_risk_penalty_applied_once_and_capped_at_thirty(self):
        events = [event(str(i), direction="NEGATIVE", category="DILUTION", valid_until=None,
                        evidence=(replace(event().evidence[0], url=url(f"2026090100000{i}")),)) for i in (1, 2, 3)]
        result = self.score(events)
        self.assertEqual(result["news_score"], 50)
        self.assertEqual(result["risk_penalty"], 30)

    def test_unverified_positive_zero_negative_requires_review_and_title_is_not_hard_exclusion(self):
        self.assertEqual(self.score([event(verified=False)])["news_score"], 50)
        result = self.score([event(verified=False, direction="NEGATIVE")])
        self.assertTrue(result["pending_checks"])
        source = replace(event().evidence[0], title="횡령 혐의 부인")
        result = self.score(raw_evidence=[source])
        self.assertTrue(result["pending_checks"])
        self.assertFalse(result["hard_exclusions"])

    def test_future_wrong_issuer_and_conflicting_review_are_rejected(self):
        with self.assertRaises(ValueError):
            self.score([replace(event(), symbol="000001")])
        with self.assertRaises(ValueError):
            self.score([event(), event(materiality="LOW")])
        with self.assertRaises(ValueError):
            self.score(cover={**coverage(), "reviewed_at": (AS_OF + timedelta(days=1)).isoformat()})

    def test_stale_recent_review_and_insufficient_risk_window_remain_distinct(self):
        stale = {**coverage(), "observed_at": (AS_OF - timedelta(days=8)).isoformat(),
                 "reviewed_at": (AS_OF - timedelta(days=8)).isoformat()}
        self.assertIsNone(self.score(cover=stale)["news_score"])
        short_history = self.score(cover={**coverage(), "start": "2026-01-01"})
        self.assertEqual(short_history['news_score'], 50)
        self.assertFalse(short_history['risk_coverage_complete'])
        self.assertIn('RISK_HISTORY_COVERAGE_UNAVAILABLE', short_history['pending_checks'])

    def test_unrelated_review_cannot_hide_a_management_headline(self):
        source = replace(event().evidence[0], title="횡령 혐의")
        result = self.score([event(evidence=(source,))], raw_evidence=[source])
        self.assertTrue(result["pending_checks"])

    def test_verified_negative_resolution_clears_that_review_signal(self):
        source = replace(event().evidence[0], title="횡령 혐의 해소")
        result = self.score([event(category="MANAGEMENT", direction="NEGATIVE", state="RESOLVED", valid_until=None,
                                  evidence=(source,))], raw_evidence=[source])
        self.assertEqual(result["pending_checks"], ())
        self.assertEqual(result["risk_penalty"], 0)

    def test_morning_coverage_uses_korean_previous_date_not_utc_previous_date(self):
        morning = AS_OF.replace(hour=0)
        c = {**coverage(), "end": "2026-09-18", "observed_at": morning.isoformat(), "reviewed_at": morning.isoformat()}
        self.assertIsNone(score_news("005930", [], c, as_of=morning)["news_score"])

    def test_wrong_issuer_primary_evidence_cannot_verify_an_event(self):
        with self.assertRaisesRegex(ValueError, "issuer-bound"):
            event(evidence=(replace(event().evidence[0], facts=("ISSUER:000001",)),))


class UnifiedVerdictTests(unittest.TestCase):
    def financial(self, **kwargs):
        return evaluate_financial(samsung(), replace(good_financial(), **kwargs), as_of=AS_OF, annual_history=history())

    def verdict(self, financial=None, news=None, price=None, current=True):
        return evaluate_unified(financial or self.financial(), news or score_news("005930", [], coverage(), as_of=AS_OF),
                                price or {"status": "READY", "reasons": []}, provenance_current=current)

    def test_fully_verified_neutral_company_is_buy_recommendation_with_eightyfive(self):
        result = self.verdict()
        self.assertEqual(result["action_decision"], "BUY_RECOMMENDATION")
        self.assertEqual(result["total_score"], Decimal("85"))

    def test_minor_turnover_warning_does_not_blanket_hold_and_good_news_can_improve_total(self):
        weak = self.financial(receivable_turnover=(Decimal("8"), Decimal("6")))
        result = self.verdict(financial=weak, news=score_news("005930", [event()], coverage(), as_of=AS_OF))
        self.assertEqual(result["total_score"], Decimal("80.5"))
        self.assertEqual(result["action_decision"], "BUY_RECOMMENDATION")
        self.assertIn("TURNOVER_WEAKENING", result["advisories"])

    def test_hard_risk_and_financial_loss_override_scores_missing_is_pending_price_failure_watch(self):
        hard = score_news("005930", [event(direction="NEGATIVE", hard_exclusion=True)], coverage(), as_of=AS_OF)
        self.assertEqual(self.verdict(news=hard)["action_decision"], "EXCLUDED")
        self.assertEqual(self.verdict(financial=self.financial(operating_cash_flow=Decimal("0")))["action_decision"], "EXCLUDED")
        self.assertEqual(self.verdict(current=False)["action_decision"], "VERIFY_PENDING")
        self.assertEqual(self.verdict(news=score_news("005930", [], None, as_of=AS_OF))["action_decision"], "VERIFY_PENDING")
        self.assertEqual(self.verdict(price={"status": "WATCH", "reasons": ["LIQUIDITY_LOW"]})["action_decision"], "WATCH")

    def test_price_session_must_match_verified_calendar_and_previous_session(self):
        bars = make_bars(start=date(2026, 7, 2))
        last = bars[-1].trade_date
        self.assertEqual(price_review("005930", bars, as_of=AS_OF, expected_session=last)["status"], "READY")
        self.assertEqual(price_review("005930", bars, as_of=AS_OF, expected_session=last - timedelta(days=1))["status"], "VERIFY_PENDING")
        self.assertEqual(price_review("005930", bars, as_of=AS_OF)["status"], "VERIFY_PENDING")

    def test_exact_total_threshold_and_penalty_are_applied_without_forcing_buy(self):
        financial = replace(self.financial(), financial_score=Decimal("90"))
        news = {**score_news("005930", [], coverage(), as_of=AS_OF), "news_score": Decimal("40")}
        self.assertEqual(self.verdict(financial=financial, news=news)["total_score"], 75)
        self.assertEqual(self.verdict(financial=financial, news=news)["action_decision"], "BUY_RECOMMENDATION")
        self.assertEqual(self.verdict(financial=financial, news={**news, "risk_penalty": Decimal("5")})["action_decision"], "WATCH")


class IntegratedResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.sqlite3"
        self.repo = StockRepository(self.source)
        self.repo.save_securities([samsung()])
        self.repo.save_bars(make_bars(start=date(2026, 7, 2)))
        self.reader = ReadOnlyResearchRepository(self.source)
        self.data_path = Path(self.temp.name) / "research.sqlite3"
        self.store = ResearchData(self.data_path, source_database=self.source)
        DartResearchCollector(self.reader, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None).run()

    def reviewed_coverage(self):
        archive = {"symbol": "005930", "start": "2023-09-19", "end": "2026-09-19", "evidence": [],
                   "search_description": "fixture archive search", "search_complete": True}
        imported = import_reviews(self.store, {"archives": [archive]}, as_of=AS_OF)
        dart = self.store.latest("dart_cursor", "005930", as_of=AS_OF)
        c = {**coverage(), "source_refs": [imported["inputs"][0]["input_id"], dart["input_id"]]}
        return import_reviews(self.store, {"coverage": [c]}, as_of=AS_OF)

    def test_collect_financial_import_news_review_total_buy_render_and_cli_keep_source_unchanged(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.reviewed_coverage()
        imported = import_reviews(self.store, {"events": [to_json_value(event())]}, as_of=AS_OF)
        self.assertEqual(imported["imported"], 1)
        self.store.put("market_calendar", "KR", {"date": "2026-09-20", "calendar": {
            "previousBusinessDay": {"date": "2026-09-19", "integrated": {"regularMarket": True}}}}, observed_at=AS_OF)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["recommend", "--database", str(self.source), "--research-database", str(self.data_path),
                         "--as-of", AS_OF.isoformat()])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["candidates"][0]["action_decision"], "BUY_RECOMMENDATION")
        self.assertEqual(report["candidates"][0]["total_score"], "91.0")
        self.assertIn("매수 추천", render_recommendations(report))
        self.assertFalse(report["orders_enabled"])
        self.assertFalse(report["paper_account_eligible"])
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_missing_history_does_not_force_candidate_and_failed_refresh_becomes_pending(self):
        self.assertEqual(build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))["candidates"], [])
        self.reviewed_coverage()
        report = build_recommendations(self.reader, self.store, as_of=AS_OF + timedelta(days=1), expected_session=date(2026, 9, 19))
        self.assertEqual(report["evaluations"][0]["action_decision"], "VERIFY_PENDING")
        self.assertIn("DART_COLLECTION_NOT_CURRENT", report["evaluations"][0]["pending_checks"])

    def test_financial_survivor_is_visible_in_research_queue_while_buy_list_remains_empty(self):
        report = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(report['candidates'], [])
        self.assertEqual(report['research_queue'][0]['symbol'], '005930')
        self.assertEqual(report['research_queue'][0]['action_decision'], 'VERIFY_PENDING')
        self.assertIsNone(report['research_queue'][0]['news_score'])
        self.assertIsNone(report['research_queue'][0]['total_score'])
        self.assertEqual(report['research_progress']['financial_qualified'], 1)
        self.assertEqual(report['research_progress']['news_scored'], 0)
        text = render_recommendations(report)
        self.assertIn('재무 점수 상위 검증 대기 종목', text)
        self.assertIn('삼성전자 (005930) · 재무 100', text)
        self.assertIn('최근 90일 뉴스 조사 미완료', text)
        self.assertNotIn('매수 추천 (다음 거래일 조건부)', text)

    def test_research_queue_has_five_unique_pending_issuers_and_uses_financial_rank(self):
        bundle = self.store.latest('financial_bundle', '005930', as_of=AS_OF)['payload']
        for i in range(1, 7):
            symbol = f'{i:06d}'
            self.repo.save_securities([replace(samsung(), symbol=symbol, name=f'검증기업{i}')])
            self.store.put('financial_bundle', symbol, json.loads(json.dumps(bundle).replace('005930', symbol)), observed_at=AS_OF)
            self.store.put('dart_cursor', symbol, {'end': '2026-09-19', 'filings': []}, observed_at=AS_OF)
        report = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(report['candidates'], [])
        self.assertEqual([r['symbol'] for r in report['research_queue']], [f'{i:06d}' for i in range(1, 6)])
        self.assertTrue(all(r['action_decision'] == 'VERIFY_PENDING' and 'rank' not in r for r in report['research_queue']))
        self.assertEqual(report['research_progress']['financial_qualified'], 7)
        self.assertEqual(report['research_progress']['pending_reason_counts']['NEWS_RECENT_COVERAGE_UNAVAILABLE'], 7)

    def test_verified_buy_is_not_duplicated_in_pending_research_queue(self):
        self.reviewed_coverage()
        report = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(report['candidates'][0]['action_decision'], 'BUY_RECOMMENDATION')
        self.assertEqual(report['research_queue'], [])
        self.assertEqual(report['research_progress']['news_scored'], 1)

    def test_renderer_handles_legacy_saved_report_without_queue_and_shows_batch_progress(self):
        report = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        report.pop('research_queue')
        report.pop('research_progress')
        self.assertIn('추천·관찰 기준을 충족한 종목 없음', render_recommendations(report))
        report['refresh_stages'] = {'archive': {'actual_calls': 2, 'results': [
            {'symbol': '005930', 'status': 'PROVIDER_LIMIT'}, {'symbol': '000001', 'status': 'PARTIAL'}]}}
        self.assertIn('005930 검색 한도 도달, 000001 조사 중 · 요청 2회', render_recommendations(report))

    def test_news_collection_only_financial_survivors_and_bounded_search_not_history_coverage(self):
        self.repo.save_securities([replace(samsung(), symbol="000001", name="부실기업")])
        self.repo.save_financial_snapshot(replace(good_financial(), symbol="000001", operating_cash_flow=Decimal("-1")))
        client = Mock()
        client.collect.return_value = []
        result = collect_targeted_news(self.reader, self.store, client, clock=lambda: AS_OF)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(client.collect.call_args.kwargs["queries"], ("삼성전자",))
        self.assertFalse(result["results"][0]["history_review_complete"])

    def test_coverage_cannot_be_forged_with_missing_references_or_short_archive(self):
        with self.assertRaisesRegex(ValueError, "references"):
            import_reviews(self.store, {"coverage": [coverage()]}, as_of=AS_OF)
        archive = self.store.put("news_archive", "005930", {"start": "2026-01-01", "end": "2026-09-19", "search_complete": True}, observed_at=AS_OF)
        dart = self.store.latest("dart_cursor", "005930", as_of=AS_OF)["input_id"]
        with self.assertRaisesRegex(ValueError, "archive"):
            import_reviews(self.store, {"coverage": [{**coverage(), "source_refs": [archive, dart]}]}, as_of=AS_OF)

    def test_review_import_not_visible_before_actual_ingestion_and_invalid_document_atomic_validation(self):
        with self.assertRaises(ValueError):
            import_reviews(self.store, {"events": [to_json_value(event()), {"bad": "entry"}]}, as_of=AS_OF)
        self.assertEqual(self.store.records("reviewed_event", "005930", as_of=AS_OF), [])
        import_reviews(self.store, {"events": [to_json_value(event())]}, as_of=AS_OF)
        self.assertEqual(self.store.records("reviewed_event", "005930", as_of=AS_OF - timedelta(seconds=1)), [])

    def test_final_list_is_one_unique_ranked_list_with_max_five_and_no_forced_fill(self):
        self.reviewed_coverage()
        for i in range(1, 7):
            symbol = f"{i:06d}"
            self.repo.save_securities([replace(samsung(), symbol=symbol, name=f"검증기업{i}")])
            self.repo.save_bars(make_bars(symbol, start=date(2026, 7, 2)))
            bundle = self.store.latest("financial_bundle", "005930", as_of=AS_OF)["payload"]
            bundle = json.loads(json.dumps(bundle).replace("005930", symbol))
            self.store.put("financial_bundle", symbol, bundle, observed_at=AS_OF)
            self.store.put("dart_cursor", symbol, {"end": "2026-09-19", "filings": []}, observed_at=AS_OF)
            self.store.put("news_coverage", symbol, {**coverage(), "symbol": symbol}, observed_at=AS_OF)
        report = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(len(report["candidates"]), 5)
        self.assertEqual([r["rank"] for r in report["candidates"]], [1, 2, 3, 4, 5])
        self.assertEqual(len({r["symbol"] for r in report["candidates"]}), 5)

    def test_failed_forced_refresh_invalidates_current_recommendation_until_successful_retry(self):
        self.reviewed_coverage()
        self.store.put("dart_attempt", "005930", {"status": "FAILED", "error_code": "NETWORK_ERROR"}, observed_at=AS_OF)
        result = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(result["evaluations"][0]["action_decision"], "VERIFY_PENDING")

    def test_cancellation_import_requires_newer_primary_evidence_and_keeps_original(self):
        import_reviews(self.store, {"events": [to_json_value(event())]}, as_of=AS_OF)
        with self.assertRaisesRegex(ValueError, "newer primary evidence"):
            import_reviews(self.store, {"events": [to_json_value(event(state="CANCELLED", reviewed_at=AS_OF + timedelta(hours=1)))]},
                           as_of=AS_OF + timedelta(hours=1))
        self.assertEqual(len(self.store.records("reviewed_event", "005930", as_of=AS_OF + timedelta(hours=1))), 1)

    def test_review_queue_exposes_actual_source_ids_and_declared_windows(self):
        queue = review_queue(self.reader, self.store, as_of=AS_OF)
        row = queue["reviews"][0]
        self.assertTrue(row["dart_cursor_input_id"])
        self.assertEqual(row["required_news_start"], "2026-06-22")
        self.assertEqual(row['required_risk_start'], '2023-09-19')
        self.assertFalse(queue["events_automatically_verified"])

    def test_report_identity_and_source_lineage_are_deterministic(self):
        self.reviewed_coverage()
        first = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        second = build_recommendations(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19))
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertTrue(first["candidates"][0]["price_input_fingerprint"])
        self.assertGreaterEqual(len(first["candidates"][0]["source_input_ids"]), 4)

    def test_unverified_price_refresh_blocks_buy_even_with_complete_scores_and_bars(self):
        self.reviewed_coverage()
        ready=build_recommendations(self.reader,self.store,as_of=AS_OF,expected_session=date(2026,9,19))
        self.assertEqual(ready['candidates'][0]['action_decision'],'BUY_RECOMMENDATION')
        pending=build_recommendations(self.reader,self.store,as_of=AS_OF,expected_session=date(2026,9,19),price_refresh_ready=False)
        self.assertEqual(pending['candidates'],[])
        self.assertIn('PRICE_REFRESH_UNVERIFIED',pending['evaluations'][0]['pending_checks'])

    def test_actual_import_time_is_not_backdated_to_historical_analysis_time(self):
        real_import_reviews(self.store, {"events": [to_json_value(event())]}, as_of=AS_OF)
        self.assertEqual(self.store.records("reviewed_event", "005930", as_of=AS_OF), [])

    def test_calendar_uses_prior_session_before_close_and_today_after_confirmed_close(self):
        payload = {"date": "2026-09-20", "calendar": {
            "today": {"date": "2026-09-20", "integrated": {"regularMarket": {
                "startTime": "2026-09-20T09:00:00+09:00", "endTime": "2026-09-20T15:30:00+09:00"}}},
            "previousBusinessDay": {"date": "2026-09-19", "integrated": {"regularMarket": True}}}}
        morning = AS_OF.replace(hour=0)
        self.store.put("market_calendar", "KR", payload, observed_at=morning)
        self.assertEqual(market_session(self.store, as_of=morning), (date(2026, 9, 19), None))
        session, closed = market_session(self.store, as_of=AS_OF)
        self.assertEqual(session, date(2026, 9, 20))
        bars = make_bars(start=date(2026, 7, 3))
        self.assertEqual(price_review("005930", bars, as_of=AS_OF, expected_session=session, verified_close_at=closed)["status"], "READY")
        self.assertEqual(price_review("005930", bars, as_of=AS_OF, expected_session=session)["status"], "VERIFY_PENDING")

    def test_holiday_calendar_and_missing_current_calendar(self):
        payload = {"date": "2026-09-20", "calendar": {
            "today": {"date": "2026-09-20", "integrated": {}},
            "previousBusinessDay": {"date": "2026-09-18", "integrated": {"regularMarket": True}}}}
        self.store.put("market_calendar", "KR", payload, observed_at=AS_OF)
        self.assertEqual(market_session(self.store, as_of=AS_OF), (date(2026, 9, 18), None))
        self.assertEqual(market_session(self.store, as_of=AS_OF + timedelta(days=1)), (None, None))

    def test_existing_token_calendar_never_issues_tokens_or_uses_account_endpoints(self):
        payload = {"today": {"date": "2026-09-20", "integrated": {}},
                   "previousBusinessDay": {"date": "2026-09-18", "integrated": {"regularMarket": True}}}
        fetch = Mock(return_value=JsonResponse(200, {"result": payload}))
        client = ExistingTokenCalendarClient("fixture-existing-token", fetch_json=fetch)
        client._post_form = Mock(side_effect=AssertionError("OAuth token issuance forbidden"))
        self.assertEqual(client.calendar(date(2026, 9, 20)), payload)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(fetch.call_args.args[0].endswith("/api/v1/market-calendar/KR"))
        client._post_form.assert_not_called()


if __name__ == "__main__":
    unittest.main()
