from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployAssetTests(unittest.TestCase):
    def test_compose_keeps_api_private_and_secret_out_of_environment(self) -> None:
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["candidate"]', compose)
        self.assertIn('ports:\n      - "127.0.0.1:9120:9120"', compose)
        self.assertNotIn('"0.0.0.0:9120:9120"', compose)
        self.assertIn("/run/secrets/stock-api-roles", compose)
        self.assertIn("- krx-auth-key", compose)
        self.assertIn("- dart-api-key", compose)
        self.assertIn("${KRX_AUTH_KEY_HOST_FILE:?required}", compose)
        self.assertIn("${DART_API_KEY_HOST_FILE:?required}", compose)
        self.assertNotIn("STOCK_API_SHARED_SECRET:", compose)
        self.assertIn("read_only: true", compose)

    def test_container_runs_as_non_root_and_healthcheck_asserts_orders_disabled(self) -> None:
        dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("USER stock:stock", dockerfile)
        self.assertIn("/run/secrets/stock-api-roles", dockerfile)
        self.assertIn("orders_enabled", compose)
        self.assertIn("is False", compose)

    def test_scheduled_cycle_uses_key_files_and_never_order_routes(self) -> None:
        script = (ROOT / "deploy" / "jobs" / "run_stock_cycle.sh").read_text(encoding="utf-8")
        self.assertIn("--key-file /run/secrets/krx-auth-key", script)
        self.assertIn("--key-file /run/secrets/dart-api-key", script)
        self.assertIn("generate-candidates", script)
        self.assertNotIn("/v1/orders", script)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", script)

    def test_rollout_keeps_production_gateway_and_orders_out_of_scope(self) -> None:
        runbook = (ROOT / "docs" / "runbooks" / "hermes-oracle-rollout.md").read_text(encoding="utf-8")
        self.assertIn("Do not replace the existing custom Telegram gateway", runbook)
        self.assertIn("brokerage orders", runbook)
        self.assertIn("separately approved transaction", runbook)


if __name__ == "__main__":
    unittest.main()
