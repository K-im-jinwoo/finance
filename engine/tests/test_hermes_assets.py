from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HermesAssetTests(unittest.TestCase):
    def test_intraday_skill_uses_one_read_only_script_and_freshness_labels(self) -> None:
        skill = (ROOT / "hermes" / "skills" / "stock-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("stock-intraday.sh SYMBOLS", skill)
        self.assertIn("exactly once", skill)
        self.assertIn("delegate once to `stock-live-market`", skill)
        self.assertIn("one to five valid six-character", skill)
        self.assertIn("실시간 오버레이", skill)
        self.assertIn("FRESH", skill)
        self.assertIn("Never call account, holdings, buying-power, order", skill)

    def test_every_profile_has_a_distinct_enforced_role_policy(self) -> None:
        expected = {
            "stock-cio": "cio",
            "stock-market": "market",
            "stock-live-market": "live_market",
            "stock-fundamentals": "fundamentals",
            "stock-risk": "risk",
        }
        for profile, role in expected.items():
            profile_root = ROOT / "hermes" / "profiles" / profile
            self.assertTrue((profile_root / "SOUL.md").is_file())
            policy = json.loads((profile_root / "tool-policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["role"], role)
            serialized = json.dumps(policy, ensure_ascii=False).casefold()
            self.assertNotIn("post /v1/orders", serialized)
            self.assertIn("orders", serialized)
            if role != "cio":
                self.assertNotIn("/v1/holdings", serialized)
                self.assertNotIn("/v1/journal/preview", serialized)

    def test_common_skill_has_no_secret_values_or_order_workflow(self) -> None:
        skill = (ROOT / "hermes" / "skills" / "stock-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("STOCK_API_ROLE_TOKEN_FILE", skill)
        self.assertIn("screen_stored", skill)
        self.assertIn("generate_candidates", skill)
        self.assertIn("latest_report", skill)
        self.assertIn("http://127.0.0.1:9120", skill)
        self.assertNotIn("normally `http://stock-assistant:9120`", skill)
        self.assertIn("Never print", skill)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", skill)
        self.assertNotIn("OPENAI_API_KEY=", skill)

    def test_only_cio_has_a_telegram_environment_template(self) -> None:
        profiles = ROOT / "hermes" / "profiles"
        cio_env = (profiles / "stock-cio" / "env.example").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_ALLOWED_USERS", cio_env)
        self.assertIn("TELEGRAM_ALLOW_ALL_USERS=false", cio_env)
        self.assertIn("STOCK_API_ROLE_TOKEN_FILE", cio_env)
        self.assertIn("PYTHONPATH=", cio_env)
        for profile in ("stock-market", "stock-live-market", "stock-fundamentals", "stock-risk"):
            self.assertFalse((profiles / profile / "env.example").exists())

    def test_company_mode_is_visible_once_without_recursive_mentions(self) -> None:
        profiles = ROOT / "hermes" / "profiles"
        cio = (profiles / "stock-cio" / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn("각각 최대 한 번만 호출", cio)
        self.assertIn("CIO 최종판정", cio)
        self.assertIn("Telegram", cio)
        self.assertIn("세 개의 `message_agent` 호출을 동시에 시작", cio)
        self.assertIn("`stock-live-market`을 최대 한 번 호출", cio)
        self.assertIn("Telegram 같은 비 Bot Chat 세션", cio)
        self.assertIn("stock-live-market-query.sh SYMBOLS", cio)
        self.assertIn("실시간 조회에 `delegate_task`를 사용하지 않는다", cio)
        self.assertIn("중간 상태 메시지를 보내지 않는다", cio)
        self.assertIn("네 개의 `message_agent` 호출을 동시에 시작", cio)
        self.assertIn("순수한 저장 보고서 재현", cio)
        self.assertIn("실시간 오버레이", cio)
        self.assertIn("실시간 오버레이 — 저장 보고서와 분리", cio)
        self.assertIn("저장 보고서·저장 종가·후보 순위에는 반영하지 않은 실시간 조회입니다.", cio)
        self.assertIn("고정 동기 브리지를 예외로 사용", cio)
        self.assertIn("추가 `message_agent` 호출", cio)
        self.assertIn("CIO가 그 메모를 인용·요약·재출력하지 않는다", cio)
        self.assertIn("전문가 메모 섹션을 다시 만들지 않는다", cio)
        self.assertIn("웹·DART·외부 데이터 재조회 금지", cio)
        for profile in ("stock-market", "stock-fundamentals", "stock-risk"):
            soul = (profiles / profile / "SOUL.md").read_text(encoding="utf-8")
            self.assertIn("다른 에이전트를 호출하거나", soul)
            self.assertIn("메모 한 번으로 끝낸다", soul)
            self.assertIn("도구 호출문", soul)
            self.assertIn("저장 보고서 검토에서는", soul)
            self.assertIn("8.71 → 13.95", soul)

    def test_live_market_profile_is_bounded_and_does_not_decide(self) -> None:
        root = ROOT / "hermes" / "profiles" / "stock-live-market"
        soul = (root / "SOUL.md").read_text(encoding="utf-8")
        wrapper = (root / "scripts" / "stock-intraday.sh").read_text(encoding="utf-8")
        policy = json.loads((root / "tool-policy.json").read_text(encoding="utf-8"))
        self.assertIn("1~5개만 조회", soul)
        self.assertIn("정확히 한 번만 실행", soul)
        self.assertIn("터미널 도구로", soul)
        self.assertIn("`execute_code`, `tool_search`, `skill_view`", soul)
        self.assertIn("`LIVE_DATA_UNAVAILABLE`", soul)
        self.assertIn("매수·매도·보유 판정을 내리지 않는다", soul)
        self.assertIn("다른 에이전트를 호출하거나", soul)
        self.assertIn("종목명, 종목코드, 현재가", soul)
        self.assertIn('if [ "$#" -lt 1 ] || [ "$#" -gt 5 ]', wrapper)
        self.assertIn('if [ "${#symbol}" -ne 6 ]', wrapper)
        self.assertIn("exec sh /srv/stock-assistant/current/deploy/jobs/stock-intraday.sh", wrapper)
        self.assertEqual(policy["role"], "live_market")
        serialized = json.dumps(policy, ensure_ascii=False).casefold()
        self.assertIn("toss read-only", serialized)
        self.assertIn("orders", serialized)

    def test_cio_live_market_bridge_is_bounded_and_synchronous(self) -> None:
        root = ROOT / "hermes" / "profiles" / "stock-cio"
        bridge = (root / "scripts" / "stock-live-market-query.sh").read_text(encoding="utf-8")
        policy = json.loads((root / "tool-policy.json").read_text(encoding="utf-8"))
        self.assertIn('if [ "$#" -lt 1 ] || [ "$#" -gt 5 ]', bridge)
        self.assertIn('if [ "${#symbol}" -ne 6 ]', bridge)
        self.assertIn("timeout 65s /home/ubuntu/.local/bin/hermes -p stock-live-market chat", bridge)
        self.assertIn('-c "Bot Chat"', bridge)
        self.assertIn("--run-budget 55", bridge)
        self.assertIn("LIVE_DATA_UNAVAILABLE", bridge)
        self.assertIn('2>"$stderr_file"', bridge)
        self.assertNotIn("delegate_task", bridge)
        self.assertIn(
            "EXEC /home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-live-market-query.sh <1-5 symbols>",
            policy["allowed"],
        )

    def test_latest_report_is_read_only_and_internal_code_is_hidden(self) -> None:
        skill = (ROOT / "hermes" / "skills" / "stock-research" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("latest_report()", skill)
        self.assertIn("must not generate a new report", skill)
        self.assertIn("Never print Python source", skill)
        self.assertIn("future timestamp", skill)
        self.assertIn("Do not browse the web, open DART pages", skill)
        self.assertIn("must not quote, summarize, or reproduce those memos", skill)
        self.assertIn("Never emit `@user`, `@cio`, or `@stock-*`", skill)
        self.assertIn("실시간 오버레이 — 저장 보고서와 분리", skill)
        self.assertIn("저장 보고서·저장 종가·후보 순위에는 반영하지 않은 실시간 조회입니다.", skill)


if __name__ == "__main__":
    unittest.main()
