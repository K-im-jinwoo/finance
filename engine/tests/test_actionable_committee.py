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

from stock_assistant.candidate_charts import prepare_chart
from stock_assistant.dart_research import DartResearchCollector
from stock_assistant.models import Evidence, to_json_value
from stock_assistant.news_archive import collect_actionable_news
from stock_assistant.providers.news import NewsArticle, NewsPage
from stock_assistant.repository import StockRepository
from stock_assistant.research_data import ResearchData
from stock_assistant.research_focus import choose_actionable_focus
from stock_assistant.research_repository import ReadOnlyResearchRepository
from stock_assistant.unified_cycle import refresh_unified
from stock_assistant.unified_research import build_recommendations, render_recommendations
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
        self.assertIn("삼성전자 (005930) · 매수 추천 (다음 거래일 조건부) · 모멘텀 지속", rendered)
        self.assertIn("⚠️ 확인 필요: 희석(유상증자/CB/BW)", rendered)
        self.assertNotIn("재무 점수 상위 검증 대기 종목", rendered)

        # Verify candidate_charts.prepare_chart accepts this actionable fallback-session candidate seamlessly!
        chart_data = prepare_chart(report, cand, self.reader)
        self.assertEqual(chart_data["symbol"], "005930")
        self.assertEqual(chart_data["session"], date(2026, 9, 19))

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


if __name__ == "__main__":
    unittest.main()
