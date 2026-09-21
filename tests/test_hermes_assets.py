from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HermesAssetTests(unittest.TestCase):
    def test_every_profile_has_a_distinct_enforced_role_policy(self) -> None:
        expected = {
            "stock-cio": "cio",
            "stock-market": "market",
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
        self.assertIn("Never print", skill)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", skill)
        self.assertNotIn("OPENAI_API_KEY=", skill)

    def test_only_cio_has_a_telegram_environment_template(self) -> None:
        profiles = ROOT / "hermes" / "profiles"
        cio_env = (profiles / "stock-cio" / "env.example").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_ALLOWED_USERS", cio_env)
        self.assertIn("TELEGRAM_ALLOW_ALL_USERS=false", cio_env)
        self.assertIn("STOCK_API_ROLE_TOKEN_FILE", cio_env)
        for profile in ("stock-market", "stock-fundamentals", "stock-risk"):
            self.assertFalse((profiles / profile / "env.example").exists())


if __name__ == "__main__":
    unittest.main()
