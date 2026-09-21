from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployAssetTests(unittest.TestCase):
    def test_compose_keeps_api_private_and_secret_out_of_environment(self) -> None:
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["candidate"]', compose)
        self.assertIn("expose:\n      - \"9120\"", compose)
        self.assertNotIn("ports:", compose)
        self.assertIn("/run/secrets/stock-api-key", compose)
        self.assertNotIn("STOCK_API_SHARED_SECRET:", compose)
        self.assertIn("read_only: true", compose)

    def test_container_runs_as_non_root_and_healthcheck_asserts_orders_disabled(self) -> None:
        dockerfile = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("USER stock:stock", dockerfile)
        self.assertIn("orders_enabled", compose)
        self.assertIn("is False", compose)

    def test_rollout_keeps_production_gateway_and_orders_out_of_scope(self) -> None:
        runbook = (ROOT / "docs" / "runbooks" / "hermes-oracle-rollout.md").read_text(encoding="utf-8")
        self.assertIn("Do not replace the existing custom Telegram gateway", runbook)
        self.assertIn("brokerage orders", runbook)
        self.assertIn("separately approved transaction", runbook)


if __name__ == "__main__":
    unittest.main()

