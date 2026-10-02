from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeployAssetTests(unittest.TestCase):
    def test_toss_credential_installer_uses_hidden_prompt_and_mode_600(self) -> None:
        installer = (ROOT / "deploy" / "install_toss_credentials.sh").read_text(encoding="utf-8")
        self.assertIn('read -rsp "Toss client_id: "', installer)
        self.assertIn('read -rsp "Toss client_secret: "', installer)
        self.assertIn('chmod 600 "$tmp_id" "$tmp_secret"', installer)
        self.assertNotIn('echo "$toss_client', installer)

    def test_intraday_wrapper_is_read_only_and_uses_secret_files(self) -> None:
        script = (ROOT / "deploy" / "jobs" / "run_intraday_watch.sh").read_text(encoding="utf-8")
        self.assertIn("refresh-intraday", script)
        self.assertIn("/run/secrets/toss-client-id", script)
        self.assertIn("/run/secrets/toss-client-secret", script)
        self.assertIn("/srv/stock-assistant/candidate.env", script)
        self.assertNotIn("orders", script.casefold())
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("TOSS_CLIENT_ID_HOST_FILE", compose)
        self.assertIn("TOSS_CLIENT_SECRET_HOST_FILE", compose)
        alerts = (ROOT / "deploy" / "jobs" / "stock-intraday-alerts.sh").read_text(encoding="utf-8")
        self.assertIn("alerts-only", alerts)
        self.assertIn("Asia/Seoul", alerts)
        self.assertIn('"$local_hhmm" -gt 1530', alerts)
        self.assertNotIn('"$local_hhmm" -gt 2000', alerts)

    def test_compose_keeps_api_private_and_secret_out_of_environment(self) -> None:
        compose = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
        candidate_env = (ROOT / "deploy" / "candidate.env.example").read_text(encoding="utf-8")
        self.assertIn("COMPOSE_PROJECT_NAME=stock-assistant", candidate_env)
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
        self.assertIn("/srv/stock-assistant/candidate.env", script)
        self.assertIn("--key-file /run/secrets/krx-auth-key", script)
        self.assertIn("--key-file /run/secrets/dart-api-key", script)
        self.assertIn("enrich-dart-disclosures", script)
        self.assertIn("evaluate-all-performance", script)
        self.assertIn("generate-candidates", script)
        self.assertNotIn("/v1/orders", script)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", script)

    def test_hermes_schedule_wrappers_use_host_config_and_fixed_modes(self) -> None:
        jobs = ROOT / "deploy" / "jobs"
        for name, mode in (
            ("stock-morning.sh", "morning"),
            ("stock-evening.sh", "evening"),
            ("stock-weekly.sh", "weekly"),
        ):
            script = (jobs / name).read_text(encoding="utf-8")
            self.assertIn("/srv/stock-assistant/schedule.env", script)
            self.assertIn(f"exec sh /srv/stock-assistant/current/deploy/jobs/run_stock_cycle.sh {mode}", script)
            self.assertNotIn("TELEGRAM_BOT_TOKEN", script)
            self.assertNotIn("/v1/orders", script)

    def test_candidate_smoke_is_reproducible_and_does_not_embed_secrets(self) -> None:
        smoke = (ROOT / "deploy" / "smoke_candidate.py").read_text(encoding="utf-8")
        self.assertIn("unauthenticated_holdings_401", smoke)
        self.assertIn("specialist_holdings_403", smoke)
        self.assertIn("specialist_same_report", smoke)
        self.assertIn("orders_403", smoke)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", smoke)
        self.assertNotIn("crtfc_key", smoke)

    def test_rollout_keeps_production_gateway_and_orders_out_of_scope(self) -> None:
        runbook = (ROOT / "docs" / "runbooks" / "hermes-oracle-rollout.md").read_text(encoding="utf-8")
        self.assertIn("Do not replace the existing custom Telegram gateway", runbook)
        self.assertIn("brokerage orders", runbook)
        self.assertIn("separately approved transaction", runbook)


if __name__ == "__main__":
    unittest.main()
