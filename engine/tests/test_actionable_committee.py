"""Offline tests for Option B: actionable price-first screening + Investment Committee brief."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from stock_assistant.candidate_charts import candidate_charts, prepare_chart, telegram_attachments
from stock_assistant.dart_research import DartResearchCollector
from stock_assistant.models import Evidence, to_json_value
from stock_assistant.news_archive import collect_actionable_news
from stock_assistant.providers.news import NewsArticle, NewsPage
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData
from stock_assistant.research_focus import choose_actionable_focus
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.unified_cycle import refresh_unified
from stock_assistant.unified_research import POLICY_VERSION_ACTIONABLE, build_recommendations, render_recommendations
from tests.helpers import make_bars
from tests.test_dart_research import FakeDart
from tests.test_screening import AS_OF, samsung


ROOT = Path(__file__).resolve().parents[1]
BRIEF_PATH = ROOT / "hermes/profiles/stock-cio/scripts/stock-committee-brief.py"
brief_spec = importlib.util.spec_from_file_location("stock_committee_brief", BRIEF_PATH)
committee = importlib.util.module_from_spec(brief_spec)
brief_spec.loader.exec_module(committee)

SCRIPT = ROOT / "deploy/jobs/run_stock_cycle.sh"
GIT_SH = Path("C:/Program Files/Git/bin/bash.exe")
SHELL = str(GIT_SH) if os.name == "nt" and GIT_SH.exists() else (shutil.which("sh") if os.name != "nt" else None)


def _bars_with_volume(symbol: str, *, volume: int, start: date = date(2026, 7, 2), rising: bool = True):
    bars = make_bars(symbol, start=start)
    if rising:
        return [replace(b, volume=volume) for b in bars]
    # Flat/downtrend bars that fail both BOTTOM_REBOUND and MOMENTUM_CONTINUATION.
    out = []
    for idx, b in enumerate(bars):
        price = Decimal("50000") - Decimal(idx * 50)
        out.append(replace(b, open=price, high=price + Decimal("100"), low=price - Decimal("100"), close=price, volume=volume))
    return out


class ActionableScreeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "source.sqlite3"
        self.repo = StockRepository(self.source)
        self.reader = ReadOnlyResearchRepository(self.source)
        self.research_path = Path(self.tmp.name) / "research.sqlite3"
        self.store = ResearchData(self.research_path, source_database=self.source)
        self.calendar = Mock()
        self.calendar.calendar.return_value = {
            "today": {"date": "2026-09-20", "integrated": {"regularMarket": {
                "startTime": "2026-09-20T09:00:00+09:00", "endTime": "2026-09-20T15:30:00+09:00"}}},
            "previousBusinessDay": {"date": "2026-09-19", "integrated": {"regularMarket": True}},
        }

    def _seed_bundle(self, symbol: str, name: str, *, volume: int = 200_000, rising: bool = True,
                     ocf_scale: Decimal = Decimal("1"), cursor_end: str = "2026-09-19"):
        sec = replace(samsung(), symbol=symbol, name=name)
        self.repo.save_securities([sec])
        self.repo.save_bars(_bars_with_volume(symbol, volume=volume, rising=rising))
        if symbol in {"005930", "000001"}:
            DartResearchCollector(self.reader, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None).run(symbols=[symbol])
        else:
            if not self.store.latest("financial_bundle", "005930", as_of=AS_OF):
                self.repo.save_securities([samsung()])
                DartResearchCollector(self.reader, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None).run(symbols=["005930"])
            template = self.store.latest("financial_bundle", "005930", as_of=AS_OF)["payload"]
            cloned = json.loads(json.dumps(template).replace("005930", symbol))
            cloned["security"]["name"] = name
            self.store.put("financial_bundle", symbol, cloned, observed_at=AS_OF)
            self.store.put("dart_cursor", symbol, {"start": "2016-09-19", "end": cursor_end, "filings": []}, observed_at=AS_OF)
        if ocf_scale != Decimal("1"):
            bundle = self.store.latest("financial_bundle", symbol, as_of=AS_OF)["payload"]
            op = Decimal(bundle["snapshot"]["operating_income"])
            bundle["snapshot"]["operating_cash_flow"] = str(op * ocf_scale)
            bundle["snapshot"]["free_cash_flow"] = str(op * ocf_scale)
            self.store.put("financial_bundle", symbol, bundle, observed_at=AS_OF)
        if cursor_end != "2026-09-19":
            cursor = self.store.latest("dart_cursor", symbol, as_of=AS_OF)["payload"]
            cursor["end"] = cursor_end
            self.store.put("dart_cursor", symbol, cursor, observed_at=AS_OF)

    def test_price_first_focus_excludes_non_ready_and_diversifies_tiers_with_cooldown(self):
        # 000100: F=100 but price NOT READY (downtrend) -> must be excluded from focus
        self._seed_bundle("000100", "비추세기업", volume=1_000_000, rising=False)
        # LARGE tier (>= 500억): close ~74,000 * 1,000,000 = 740억
        self._seed_bundle("000200", "대형기업A", volume=1_000_000, rising=True, ocf_scale=Decimal("1.2"))
        self._seed_bundle("000201", "대형기업B", volume=1_000_000, rising=True, ocf_scale=Decimal("1.8"))
        # MID tier (50억~500억): close ~74,000 * 200,000 = 148억
        self._seed_bundle("000300", "중형기업", volume=200_000, rising=True, ocf_scale=Decimal("1.5"))
        # SMALL tier (10억~50억): close ~170,000 * 10,000 = 17억
        self._seed_bundle("000400", "소형기업", volume=10_000, rising=True, ocf_scale=Decimal("1.4"))

        focus = choose_actionable_focus(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19), limit=3)
        selected_symbols = [item["symbol"] for item in focus["selected"]]
        self.assertNotIn("000100", focus["ready_pool"])
        # Round-robin across LARGE (000201 beats 000200 via cash quality tie-breaker), MID (000300), SMALL (000400)
        self.assertEqual(selected_symbols, ["000201", "000300", "000400"])

        # Put 000201 into 30-day cooldown via a prior report -> 000200 now replaces it in LARGE tier
        self.store.put("unified_report", "ALL", {
            "report_id": "UR-" + "1" * 24,
            "candidates": [{"symbol": "000201", "action_decision": "BUY_RECOMMENDATION"}],
        }, observed_at=AS_OF - timedelta(days=5))
        focus2 = choose_actionable_focus(self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19), limit=3)
        self.assertEqual([item["symbol"] for item in focus2["selected"]], ["000200", "000300", "000400"])

    def test_actionable_cycle_handles_t1_price_fallback_risk_warnings_and_chart_compatibility(self):
        self._seed_bundle("005930", "삼성전자", volume=1_000_000, rising=True)
        # Simulate KRX T+1 delay: daily_check failed for today (2026-09-20), while 2026-09-19 bars exist.
        self.repo.save_daily_check({
            "status": "FAILED", "research_status": "VERIFY_PENDING",
            "checked_at": (AS_OF - timedelta(seconds=1)).isoformat(),
            "target_date": "2026-09-20", "error_code": "KRX_DAILY_DATA_UNAVAILABLE",
        })
        risk_article = NewsArticle(
            title="삼성전자 유상증자 설 일축", url="https://news.example.com/1",
            published_at=AS_OF - timedelta(days=2), observed_at=AS_OF, description="단순 루머 해명 기사",
        )
        normal_article = NewsArticle(
            title="삼성전자 신규 반도체 수주 기대", url="https://news.example.com/2",
            published_at=AS_OF - timedelta(days=1), observed_at=AS_OF, description="업황 개선 전망",
        )
        news = Mock()
        news.archive_page.return_value = NewsPage((normal_article, risk_article), 2, 1, 2, AS_OF)

        with patch("stock_assistant.unified_cycle.review_official_contracts", return_value={"verified": [], "pending": []}):
            report = refresh_unified(
                self.reader, self.store, dart_client=FakeDart(), news_client=news,
                calendar_client=self.calendar, dart_key="fixture-only", archive_pages=5,
                clock=lambda: AS_OF, actionable=True,
            )

        self.assertEqual(report["policy_mode"], "ACTIONABLE_COMMITTEE")
        self.assertEqual(report["price_session_basis"], "LATEST_AVAILABLE_SESSION")
        self.assertEqual(report["expected_price_session"], "2026-09-19")
        self.assertEqual(len(report["candidates"]), 1)
        cand = report["candidates"][0]
        self.assertEqual(cand["symbol"], "005930")
        self.assertEqual(cand["action_decision"], "BUY_RECOMMENDATION")
        self.assertEqual(cand["total_score"], "85.0")
        self.assertFalse(report["orders_enabled"])
        self.assertTrue(any("희석(유상증자/CB/BW)" in w for w in cand["warnings"]))
        self.assertTrue(any("직전 확보 거래일(2026-09-19)" in w for w in cand["warnings"]))
        self.assertEqual(len(cand["risk_headlines"]), 1)
        self.assertEqual(len(cand["recent_headlines"]), 1)

        rendered = render_recommendations(report)
        self.assertIn("장기투자 종목 리서치", rendered)
        self.assertIn("KST)", rendered)
        self.assertIn(f"보고서: {report['report_id']}", rendered)
        self.assertIn("[오늘의 후보 1종목]", rendered)
        self.assertIn("1️⃣ 삼성전자 (005930) · 매수 추천 · 모멘텀 지속", rendered)
        self.assertIn("• ⚠️ 확인: 희석(유상증자/CB/BW)", rendered)
        self.assertIn("📌 공통 진입·무효화 조건", rendered)
        self.assertEqual(rendered.count("다음 거래일 최신 시세로 유동성·가격 조건 재확인"), 1)
        self.assertNotIn("재무 점수 상위 검증 대기 종목", rendered)

        # Verify candidate_charts.prepare_chart and compact telegram_attachments work seamlessly!
        chart_data = prepare_chart(report, cand, self.reader)
        self.assertEqual(chart_data["symbol"], "005930")
        self.assertEqual(chart_data["session"], date(2026, 9, 19))
        chart_result = candidate_charts(report, self.reader, Path(self.tmp.name) / "charts", renderer=Mock())
        self.assertTrue(chart_result["compact"])
        caption = telegram_attachments(chart_result)
        self.assertIn("📈 삼성전자 (005930)", caption)
        self.assertIn("20일선 위(", caption)
        self.assertIn("MEDIA:", caption)
        self.assertNotIn("평균선 배열만으로 골든크로스를", caption)

        # Verify stock-committee-brief.py loads the real saved ResearchData report (matching APPLICATION_ID).
        loaded = committee.load_saved_report(self.research_path, report["report_id"], source_database=self.source)
        self.assertEqual(loaded["report_id"], report["report_id"])
        self.assertEqual(loaded["candidates"][0]["symbol"], "005930")

    def test_actionable_preserves_hard_exclusions(self):
        self._seed_bundle("005930", "삼성전자", volume=1_000_000, rising=True)
        # Flip OCF to negative -> hard exclusion MUST still exclude the stock
        bundle = self.store.latest("financial_bundle", "005930", as_of=AS_OF)["payload"]
        bundle["snapshot"]["operating_cash_flow"] = "-100"
        self.store.put("financial_bundle", "005930", bundle, observed_at=AS_OF)
        report = build_recommendations(
            self.reader, self.store, as_of=AS_OF, expected_session=date(2026, 9, 19), actionable=True,
        )
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["evaluations"][0]["action_decision"], "EXCLUDED")

    def test_repeated_top_five_are_presented_as_maintained(self):
        for symbol in ("005930", "000200", "000201", "000300", "000400"):
            self._seed_bundle(symbol, symbol)
        options = {"expected_session": date(2026, 9, 19), "actionable": True, "actionable_focus": {}}
        first = build_recommendations(self.reader, self.store, as_of=AS_OF, **options)
        self.assertEqual(len(first["candidates"]), 5)
        for hours in (1, 2):
            previous_time = AS_OF + timedelta(hours=hours - 1)
            self.store.put("unified_report", "ALL", first, observed_at=previous_time)
            current = build_recommendations(self.reader, self.store, as_of=AS_OF + timedelta(hours=hours), **options)
            body = render_recommendations(current)
            self.assertIn("[신규 추천·관찰 / 점수·판정 변동]\n• 없음", body)
            self.assertIn("[기존 유지 5종목]", body)
            self.assertEqual([(c["symbol"], c["total_score"]) for c in current["candidates"]],
                             [(c["symbol"], c["total_score"]) for c in first["candidates"]])
            first = current

    def test_candidate_summary_lists_new_and_changed_scores_without_losing_rank(self):
        for symbol, name in (("005930", "유지기업"), ("000200", "점수변동기업"), ("000300", "신규기업")):
            self._seed_bundle(symbol, name)
        previous_at = AS_OF - timedelta(days=1)
        self.store.put("unified_report", "ALL", {
            "report_id": "UR-" + "1" * 24, "as_of": previous_at.isoformat(), "policy_version": POLICY_VERSION_ACTIONABLE,
            "candidates": [{"symbol": "005930", "total_score": "85", "action_decision": "BUY_RECOMMENDATION"},
                           {"symbol": "000200", "total_score": "84", "action_decision": "WATCH"}],
        }, observed_at=previous_at)
        report = build_recommendations(self.reader, self.store, as_of=AS_OF,
                                       expected_session=date(2026, 9, 19), actionable=True, actionable_focus={})
        body = render_recommendations(report)
        self.assertIn("1. 점수변동기업 (000200) · 총점 85.0 · 84 → 85.0 · 관찰 → 매수 추천", body)
        self.assertIn("2. 신규기업 (000300) · 총점 85.0 · 신규 추천", body)
        self.assertIn("[기존 유지 1종목]\n3. 유지기업 (005930)", body)

    def test_candidate_news_dates_and_missing_warning_are_visible_after_successful_collection(self):
        for symbol, name in (("005930", "미수집기업"), ("000200", "오래된기업"), ("000300", "최신기업"),
                             ("000400", "며칠전수집기업")):
            self._seed_bundle(symbol, name)
        for symbol, age, end in (("000200", 8, "2026-09-11"), ("000300", 0, "2026-09-19"),
                                 ("000400", 6, "2026-09-13")):
            observed_at = AS_OF - timedelta(days=age)
            self.store.put("recent_news_snapshot", symbol, {"end": end, "status": "SNAPSHOT_100"}, observed_at=observed_at)
        report = build_recommendations(self.reader, self.store, as_of=AS_OF,
                                       expected_session=date(2026, 9, 19), actionable=True, actionable_focus={})
        report["refresh_stages"] = {"archive": {"status": "SNAPSHOT_100", "actual_calls": 5}}
        body = render_recommendations(report)
        self.assertIn("뉴스 기준일: 2026-09-11 · ⚠️ 2026-09-19 기준 미갱신", body)
        self.assertIn("뉴스 기준일: 2026-09-13 · ⚠️ 2026-09-19 기준 미갱신", body)
        self.assertIn("뉴스 기준일: 2026-09-19\n", body)
        self.assertIn("뉴스 기준일: 확인 불가", body)
        self.assertEqual(body.count("최근 뉴스 미수집(중립 50점 출발)"), 2)
        self.assertNotIn("최근 기사 수집 완료", body)

    def test_older_saved_report_keeps_existing_missing_news_warning(self):
        self._seed_bundle("005930", "삼성전자")
        report = build_recommendations(self.reader, self.store, as_of=AS_OF,
                                       expected_session=date(2026, 9, 19), actionable=True)
        report.pop("candidate_comparison", None)
        candidate = report["candidates"][0]
        candidate.pop("news_reference_date", None)
        candidate["warnings"] = ["⚠️ 확인 필요: 최근 뉴스 미수집(중립 50점 기준)"]
        body = render_recommendations(report)
        self.assertIn("뉴스 기준일: 확인 불가", body)
        self.assertIn("최근 뉴스 미수집(중립 50점 기준)", body)

    def test_dart_collector_prioritizes_actionable_symbols_first(self):
        self._seed_bundle("000001", "일반기업1", cursor_end="2026-09-10")
        self._seed_bundle("005930", "우선후보", cursor_end="2026-09-10")
        result = DartResearchCollector(
            self.reader, self.store, FakeDart(), clock=lambda: AS_OF, sleeper=lambda _: None
        ).run(batch_size=1, priority_symbols=["005930"])
        self.assertEqual([r["symbol"] for r in result["results"]], ["005930"])


class CommitteeBriefTests(unittest.TestCase):
    def _sample_report(self):
        return {
            "contract": "UNIFIED_RESEARCH_V1",
            "report_id": "UR-" + "a" * 24,
            "as_of": AS_OF.isoformat(),
            "candidates": [{
                "rank": 1,
                "symbol": "012330",
                "name": "현대모비스",
                "action_decision": "BUY_RECOMMENDATION",
                "label": "매수 추천 (다음 거래일 조건부)",
                "financial_score": "100",
                "news_score": "50",
                "risk_penalty": "0",
                "total_score": "85.0",
                "liquidity_tier": "LARGE",
                "price": {"status": "READY", "strategy": "BOTTOM_REBOUND", "session": "2026-09-19", "metrics": {}},
                "warnings": ["⚠️ 확인 필요: 희석(유상증자/CB/BW) 관련 기사/공시 1건"],
                "advisories": [],
                "recent_headlines": [{"title": "현대모비스 수주 확대", "url": "https://news.example.com/10"}],
                "risk_headlines": [{"title": "관련 업계 전환사채 이슈", "url": "https://news.example.com/11"}],
                "recent_dart_filings": [{"title": "현대모비스 - 사업보고서", "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260320000001"}],
            }],
        }

    def test_no_candidates_skips_llm_calls(self):
        runner = Mock()
        result = committee.generate_committee_brief(
            {"contract": "UNIFIED_RESEARCH_V1", "report_id": "UR-" + "a" * 24, "as_of": AS_OF.isoformat(), "candidates": []},
            specialist_runner=runner, cio_runner=runner, now=AS_OF,
        )
        self.assertEqual(result["status"], "NO_CANDIDATES")
        self.assertEqual(result["brief"], "")
        runner.assert_not_called()

    def test_committee_runs_three_specialists_and_cio_synthesis(self):
        seen_profiles = []

        def fake_specialist(profile, prompt, directory):
            seen_profiles.append(profile)
            return {"profile": profile, "status": "COMPLETE", "memo": f"{profile} 검토 완료"}

        def fake_cio(profile, prompt, directory):
            seen_profiles.append(profile)
            self.assertIn("현대모비스", prompt)
            return {"profile": profile, "status": "COMPLETE", "memo": "투자위원회 요약\n- [1] 현대모비스 (012330)\n  • Bull: 재무 100점·바닥반등\n  • Bear/위험: 단순 업계 언급으로 실악재 아님"}

        result = committee.generate_committee_brief(
            self._sample_report(), specialist_runner=fake_specialist, cio_runner=fake_cio, now=AS_OF,
        )
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(set(seen_profiles), {"stock-market", "stock-fundamentals", "stock-risk", "stock-cio"})
        self.assertIn("현대모비스", result["brief"])
        self.assertIn("전문 검토: 시장(완료) · 재무(완료) · 리스크(완료)", result["brief"])
        self.assertFalse(result["orders_enabled"])

    def test_cio_failure_falls_back_to_completed_specialist_memos(self):
        def fake_specialist(profile, prompt, directory):
            if profile == "stock-risk":
                return {"profile": profile, "status": "UNAVAILABLE", "memo": None, "error_code": "EXPERT_TIMEOUT"}
            return {"profile": profile, "status": "COMPLETE", "memo": f"{profile} 핵심 요약"}

        def fake_cio(profile, prompt, directory):
            return {"profile": profile, "status": "UNAVAILABLE", "memo": None, "error_code": "EXPERT_TIMEOUT"}

        result = committee.generate_committee_brief(
            self._sample_report(), specialist_runner=fake_specialist, cio_runner=fake_cio, now=AS_OF,
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("stock-market 핵심 요약", result["brief"])
        self.assertIn("전문 검토: 시장(완료) · 재무(완료) · 리스크(미완료)", result["brief"])

    def test_all_specialists_unavailable_returns_unavailable(self):
        def failing(profile, prompt, directory):
            return {"profile": profile, "status": "UNAVAILABLE", "memo": None, "error_code": "EXPERT_TIMEOUT"}

        result = committee.generate_committee_brief(
            self._sample_report(), specialist_runner=failing, cio_runner=failing, now=AS_OF,
        )
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_default_committee_runner_passes_medium_reasoning_and_extended_budget(self):
        captured = []

        class FakeBridge:
            def run_specialist(self, profile, prompt, directory, **kwargs):
                captured.append((profile, kwargs))
                return {"profile": profile, "status": "COMPLETE", "memo": f"{profile} ok"}

            def review(self, request, *, runner, now=None):
                specs = [runner(p, "prompt", Path(".")) for p in ("stock-market", "stock-fundamentals", "stock-risk")]
                return {"status": "COMPLETE", "specialists": specs}

        result = committee.generate_committee_brief(self._sample_report(), bridge=FakeBridge(), now=AS_OF)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(len(captured), 4)
        for _profile, kwargs in captured:
            self.assertEqual(kwargs["reasoning"], "medium")
            self.assertEqual(kwargs["budget"], 120)
            self.assertEqual(kwargs["timeout"], 140)


@unittest.skipUnless(SHELL, "POSIX shell is unavailable")
class ActionableCycleShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.docker_args = self.root / "docker-args.txt"
        self.brief_args = self.root / "brief-args.txt"
        self.envfile = self.root / "candidate.env"
        self.envfile.write_text("# offline fixture\n", encoding="utf-8")
        self.report_id = "UR-" + "b" * 24
        self.report = f"통합 종목 연구 · 다음 거래일 조건부\n보고서: {self.report_id}\n1. 현대모비스 (012330)"

        docker = self.bin / "docker"
        docker.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_ARGS_FILE"
case "$*" in
  *refresh-daily*) exit 0 ;;
  *refresh-unified*) printf '%s\\n' "$FAKE_REPORT" ;;
  *evaluate-all-performance*) exit 0 ;;
  *) exit 33 ;;
esac
""", encoding="utf-8", newline="\n")
        brief_py = self.bin / "brief-python"
        brief_py.write_text("""#!/bin/sh
printf '%s\\n' "$*" > "$BRIEF_ARGS_FILE"
if [ "${FAKE_BRIEF_EXIT:-0}" != 0 ]; then exit "$FAKE_BRIEF_EXIT"; fi
printf '투자위원회 요약\\n- [1] 현대모비스 Bull/Bear\\n'
""", encoding="utf-8", newline="\n")
        docker.chmod(0o755)
        brief_py.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "STOCK_CANDIDATE_ENV_FILE": self.envfile.as_posix(),
            "STOCK_UNIFIED_ENABLED": "true",
            "STOCK_NEWS_ENABLED": "false",
            "STOCK_CANDIDATE_CHARTS": "false",
            "STOCK_ACTIONABLE_ENABLED": "true",
            "STOCK_COMMITTEE_BRIEF": "true",
            "STOCK_COMMITTEE_PYTHON": brief_py.as_posix(),
            "STOCK_COMMITTEE_SCRIPT": "/fixture/stock-committee-brief.py",
            "DOCKER_ARGS_FILE": self.docker_args.as_posix(),
            "BRIEF_ARGS_FILE": self.brief_args.as_posix(),
            "FAKE_REPORT": self.report,
        }

    def test_shell_passes_actionable_and_emits_committee_brief(self):
        res = subprocess.run([SHELL, SCRIPT.as_posix(), "evening"], env=self.env,
                             capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("--actionable", self.docker_args.read_text(encoding="utf-8"))
        self.assertIn("--report-id " + self.report_id, self.brief_args.read_text(encoding="utf-8"))
        self.assertIn("투자위원회 요약", res.stdout)
        self.assertIn("현대모비스", res.stdout)

    def test_committee_failure_keeps_deterministic_report(self):
        res = subprocess.run([SHELL, SCRIPT.as_posix(), "evening"], env={**self.env, "FAKE_BRIEF_EXIT": "2"},
                             capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(self.report, res.stdout)
        self.assertIn("투자위원회 요약 실패", res.stdout)

    def test_daily_refresh_mode_only_refreshes_prices_silently(self):
        res = subprocess.run([SHELL, SCRIPT.as_posix(), "daily-refresh"], env=self.env,
                             capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")
        docker_calls = self.docker_args.read_text(encoding="utf-8")
        self.assertIn("refresh-daily", docker_calls)
        self.assertNotIn("refresh-unified", docker_calls)
        self.assertFalse(self.brief_args.exists())
        self.assertIn("STOCK_CYCLE status=READY mode=daily-refresh stage=DAILY_REFRESH", res.stderr)

    def test_weekly_cycle_evaluates_saved_unified_reports(self):
        res = subprocess.run([SHELL, SCRIPT.as_posix(), "weekly"], env=self.env,
                             capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self.docker_args.read_text(encoding="utf-8").splitlines()
        self.assertIn("refresh-unified", calls[1])
        self.assertIn("evaluate-all-performance", calls[2])
        self.assertIn("--research-database /var/lib/stock/unified-research-inputs.sqlite3", calls[2])


if __name__ == "__main__":
    unittest.main()
