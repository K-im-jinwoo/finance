import base64
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


FILE = Path(__file__).resolve().parents[1] / "deploy/hermes/deploy_stock_expert_review.py"
spec = importlib.util.spec_from_file_location("expert_deployment", FILE)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "profiles"
        self.backups = Path(self.temporary.name) / "backups"
        self.patch_root = patch.object(deployment, "PROFILE_ROOT", self.root)
        self.patch_backup = patch.object(deployment, "BACKUP_ROOT", self.backups)
        self.patch_root.start()
        self.patch_backup.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_backup.stop)
        self.request = {"approval": "CONNECT_STOCK_EXPERTS_TO_TELEGRAM", "files": {}}
        for name in deployment.RELATIVE_FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not name.endswith(".py"):
                target.write_text("original " + name, encoding="utf-8")
            raw = b"# STOCK_EXPERT_REVIEW_V1\n" if name.endswith(".py") else b"STOCK_EXPERT_REVIEW_V1"
            if name.endswith(".json"):
                raw = json.dumps({"role": "cio", "allowed": ["GET /v1/reports/latest"], "forbidden": ["orders"]}).encode()
            self.request["files"][name] = {"before_sha256": deployment.state(target),
                                           "base64": base64.b64encode(raw).decode(),
                                           "sha256": deployment.digest(raw)}
        for name in deployment.PRESERVED_FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("preserved synthetic config", encoding="utf-8")
        self.config = self.root / "stock-cio/config.yaml"
        self.original_config = b"model: fixture-model\napprovals:\n  single_query_mode: reject\n"
        self.config.write_bytes(self.original_config)
        self.request["cio_config_before_sha256"] = deployment.state(self.config)
        # The host already has Hermes' YAML parser. These portable Windows tests
        # stub that dependency; the real parser is checked again before writes.
        def fixture_yaml(raw):
            value = {"model": "fixture-model", "approvals": {"single_query_mode": "reject"}}
            if b"mcp_servers:" in raw:
                value["mcp_servers"] = {"stock_experts": deployment.MCP_ENTRY}
            return value
        decoder = patch.object(deployment, "read_yaml", side_effect=fixture_yaml)
        decoder.start()
        self.addCleanup(decoder.stop)

    def test_only_exact_approved_payload_and_current_hashes_are_accepted(self):
        decoded = deployment.validate_payload(self.request)
        self.assertEqual(set(decoded), set(deployment.RELATIVE_FILES) | {"stock-cio/config.yaml"})
        self.assertTrue(decoded["stock-cio/config.yaml"].startswith(self.original_config))
        for changed in ({**self.request, "approval": "other"},
                        {**self.request, "files": {}},
                        {**self.request, "files": {**self.request["files"], "stock-cio/.env": {}}}):
            with self.assertRaises(ValueError):
                deployment.validate_payload(changed)
        (self.root / "stock-market/SOUL.md").write_text("new user change")
        with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
            deployment.validate_payload(self.request)

    def test_payload_hash_or_order_permission_cannot_be_smuggled_in(self):
        entry = self.request["files"]["stock-cio/tool-policy.json"]
        entry["base64"] = base64.b64encode(b"changed").decode()
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            deployment.validate_payload(self.request)
        raw = json.dumps({"role": "cio", "allowed": ["POST /v1/orders"]}).encode()
        entry.update(base64=base64.b64encode(raw).decode(), sha256=deployment.digest(raw))
        with self.assertRaisesRegex(ValueError, "ORDER_TOOL_FORBIDDEN"):
            deployment.validate_payload(self.request)

    def test_success_preserves_unrelated_files_and_creates_verified_backup(self):
        before = {name: deployment.state(self.root / name) for name in deployment.PRESERVED_FILES}
        metadata = {"session_id": "fixture", "latest_message_id": 5, "messages": 5, "prompt_cache_empty": True}

        def service(action=None):
            return {"ActiveState": "inactive" if action == "stop" else "active", "SubState": "running"}

        with patch.object(deployment, "service", side_effect=service), \
                patch.object(deployment, "health", return_value={"status": "ok", "orders_enabled": False}), \
                patch.object(deployment, "telegram_state", return_value=metadata), \
                patch.object(deployment, "host_owner", return_value=SimpleNamespace(pw_uid=0, pw_gid=0)), \
                patch.object(deployment, "atomic", side_effect=lambda p, raw, *_: p.write_bytes(raw)):
            result = deployment.rollout(self.request)
        self.assertTrue(result["deployed"])
        self.assertEqual(result["database_writes"], 0)
        self.assertEqual(result["prompt_cache_writes"], 0)
        self.assertEqual(result["telegram_messages_sent"], 0)
        self.assertTrue(self.config.read_bytes().startswith(self.original_config))
        self.assertIn(b"single_query_mode: reject", self.config.read_bytes())
        self.assertIn(b"--mcp", self.config.read_bytes())
        self.assertEqual(before, {name: deployment.state(self.root / name) for name in deployment.PRESERVED_FILES})
        self.assertTrue((Path(result["backup"]) / "stock-market/SOUL.md").is_file())

    def test_failure_restores_only_our_written_files_and_removes_only_new_bridge(self):
        before = {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                  for name in (*deployment.RELATIVE_FILES, "stock-cio/config.yaml")}
        metadata = {"session_id": "fixture", "latest_message_id": 5, "messages": 5, "prompt_cache_empty": True}
        calls = []

        def service(action=None):
            calls.append(action)
            if action == "start" and calls.count("start") == 1:
                raise RuntimeError("fixture restart failed")
            return {"ActiveState": "inactive" if action == "stop" else "active", "SubState": "running"}

        with patch.object(deployment, "service", side_effect=service), \
                patch.object(deployment, "health", return_value={"status": "ok", "orders_enabled": False}), \
                patch.object(deployment, "telegram_state", return_value=metadata), \
                patch.object(deployment, "host_owner", return_value=SimpleNamespace(pw_uid=0, pw_gid=0)), \
                patch.object(deployment, "atomic", side_effect=lambda p, raw, *_: p.write_bytes(raw)), \
                self.assertRaisesRegex(RuntimeError, "restart failed"):
            deployment.rollout(self.request)
        after = {name: (self.root / name).read_bytes() if (self.root / name).exists() else None
                 for name in (*deployment.RELATIVE_FILES, "stock-cio/config.yaml")}
        self.assertEqual(before, after)
        self.assertEqual(calls.count("start"), 2)

    def test_busy_or_cached_conversation_prevents_any_mutation(self):
        with patch.object(deployment, "service", return_value={"ActiveState": "active", "SubState": "running"}), \
                patch.object(deployment, "health", return_value={}), \
                patch.object(deployment, "telegram_state", side_effect=ValueError("TELEGRAM_CONVERSATION_BUSY")), \
                patch.object(deployment, "atomic") as write, self.assertRaises(ValueError):
            deployment.rollout(self.request)
        write.assert_not_called()
        self.assertFalse(self.backups.exists())

    def test_pending_assistant_tool_call_is_busy_and_requires_a_final_answer(self):
        path = self.root / "stock-cio/state.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE sessions (id TEXT, source TEXT, system_prompt TEXT, system_prompt_hash TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                                   tool_calls TEXT, content TEXT, timestamp REAL);
            INSERT INTO sessions VALUES ('fixture', 'telegram', NULL, NULL);
        """)
        for role, calls, content in [("user", None, "review"),
                                     ("assistant", '[{"function":{"name":"terminal"}}]', ""),
                                     ("tool", None, "fixture result"),
                                     ("assistant", None, "")]:
            with self.subTest(role=role, calls=calls):
                connection.execute("INSERT INTO messages(session_id,role,tool_calls,content,timestamp) VALUES (?,?,?,?,?)",
                                   ("fixture", role, calls, content, 1))
                connection.commit()
                with self.assertRaisesRegex(ValueError, "CONVERSATION_BUSY"):
                    deployment.telegram_state()
        connection.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,?,?,?)",
                           ("fixture", "assistant", "final fixture answer", 1))
        connection.commit()
        self.assertTrue(deployment.telegram_state()["prompt_cache_empty"])
        connection.execute("UPDATE sessions SET system_prompt_hash='orphan-fixture-hash'")
        connection.commit()
        self.assertEqual(deployment.telegram_state()["orphan_prompt_hash"], "orphan-fixture-hash")
        connection.execute("UPDATE sessions SET system_prompt='cached fixture'")
        connection.commit()
        with self.assertRaisesRegex(ValueError, "PROMPT_CACHE"):
            deployment.telegram_state()
        connection.close()


if __name__ == "__main__":
    unittest.main()
