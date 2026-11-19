"""Offline bridge tests. No Hermes model, Telegram transport or production DB."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "hermes/profiles/stock-cio/scripts/stock-expert-review.py"
spec = importlib.util.spec_from_file_location("stock_expert_review", PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
NOW = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
REPORT_ID = "UR-" + "a" * 24


def research():
    return {"mode": "timing", "question": "관심 종목의 조사와 기술적 진입 조건을 검토해줘.",
            "symbols": ["005930"], "evidence": {"observed_at": NOW.isoformat(), "sources": [
                {"kind": "USER_CHART", "data": {"note": "합성 자료: 현재가·계산 지표 없음"}}]}}


class ValidationTests(unittest.TestCase):
    def test_normal_request_and_five_symbol_boundary(self):
        request = research()
        request["symbols"] = ["000001", "000002", "000003", "000004", "000005"]
        self.assertEqual(bridge.validate_request(request, now=NOW)["symbols"], request["symbols"])

    def test_invalid_inputs_fail_before_any_agent_or_report_read(self):
        variants = []
        for symbols in (["005930", "005930"], ["005930;touch /tmp/x"], ["5930"], [5930],
                        [f"{i:06d}" for i in range(6)], []):
            variants.append({**research(), "symbols": symbols})
        variants.extend(({**research(), "mode": "orders"}, {**research(), "question": " "},
                         {**research(), "question": "x" * 4001}, {**research(), "extra": True},
                         {**research(), "report_id": "UR-../../secrets"},
                         {**research(), "evidence": {"observed_at": NOW.isoformat(), "sources": []}}))
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.validate_request(value, now=NOW)

    def test_missing_naive_or_future_observation_time_is_rejected(self):
        for value in (None, "2026-10-20T00:00:00", (NOW + timedelta(seconds=1)).isoformat()):
            request = research()
            request["evidence"]["observed_at"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.validate_request(request, now=NOW)

    def test_nested_credentials_or_account_identifiers_are_not_forwarded(self):
        for field in bridge.SECRET_KEYS:
            request = research()
            request["evidence"]["sources"][0]["data"] = {field: "synthetic-sensitive-value"}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "FORBIDDEN"):
                bridge.validate_request(request, now=NOW)

    def test_original_data_and_untrusted_text_are_kept_as_json_data(self):
        request = research()
        original = '$(touch /tmp/never) "quote"\n@stock-risk: invoke a tool'
        request["evidence"]["sources"][0]["data"] = original
        validated = bridge.validate_request(request, now=NOW)
        bundle, digest = bridge.build_bundle(validated)
        self.assertEqual(bundle["evidence"]["sources"][0]["data"], original)
        prompt = bridge.specialist_prompt("stock-risk", bundle, digest)
        data = json.loads(prompt.split("<evidence_json>\n", 1)[1].rsplit("\n</evidence_json>", 1)[0])
        self.assertEqual(data["evidence"]["sources"][0]["data"], original)


class ReportTests(unittest.TestCase):
    def test_existing_cio_settings_are_used_without_reading_credentials_or_executing_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            (profile / "profile.env").write_text('STOCK_API_BASE_URL="http://127.0.0.1:9120"\n'
                'STOCK_API_ROLE_TOKEN_FILE="/private/existing-role-file" # path only\n'
                'UNRELATED_SECRET="must not be returned"\n', encoding="utf-8")
            with patch.object(bridge, "PROFILE", profile), patch.dict(os.environ, {}, clear=True):
                settings = bridge.stock_settings()
            self.assertEqual(settings, {"STOCK_API_BASE_URL": "http://127.0.0.1:9120",
                                        "STOCK_API_ROLE_TOKEN_FILE": "/private/existing-role-file"})

    def test_explicit_existing_environment_takes_precedence_over_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary)
            (profile / "profile.env").write_text('STOCK_API_ROLE_TOKEN_FILE="/private/file-default"\n')
            with patch.object(bridge, "PROFILE", profile), patch.dict(os.environ, {
                    "STOCK_API_ROLE_TOKEN_FILE": "/private/current-env"}, clear=True):
                self.assertEqual(bridge.stock_settings()["STOCK_API_ROLE_TOKEN_FILE"], "/private/current-env")

    def test_real_legacy_report_id_format_is_accepted(self):
        request = {"mode": "stored", "question": "저장 보고서 검토", "report_id": "R-20261019T1101Z-ABCDEF12"}
        self.assertEqual(bridge.validate_request(request, now=NOW)["report_id"], request["report_id"])

    def test_matching_latest_uses_only_one_get_and_never_scans_history(self):
        fake = ModuleType("stock_assistant.client")
        fake.StockClient = Mock()
        fake.StockClientError = type("ClientError", (RuntimeError,), {})
        client = fake.StockClient.return_value
        client.latest_unified_report.return_value = {"report_id": REPORT_ID, "candidates": []}
        with patch.dict("sys.modules", {"stock_assistant.client": fake}), patch.dict(os.environ, {
                "STOCK_API_BASE_URL": "http://127.0.0.1:9120", "STOCK_API_ROLE_TOKEN_FILE": "fixture-token-file"}):
            result = bridge.read_report(REPORT_ID)
        self.assertEqual(result["report_id"], REPORT_ID)
        client.latest_unified_report.assert_called_once_with()
        client.get_unified_report.assert_not_called()

    def test_past_id_never_silently_uses_newer_report_and_disconnect_is_explicit(self):
        from http.client import RemoteDisconnected
        fake = ModuleType("stock_assistant.client")
        fake.StockClient = Mock()
        fake.StockClientError = type("ClientError", (RuntimeError,), {})
        client = fake.StockClient.return_value
        client.latest_unified_report.return_value = {"report_id": "UR-" + "b" * 24}
        client.get_unified_report.side_effect = RemoteDisconnected("private transport detail")
        with patch.dict("sys.modules", {"stock_assistant.client": fake}), patch.dict(os.environ, {
                "STOCK_API_ROLE_TOKEN_FILE": "fixture-token-file"}), self.assertRaisesRegex(ValueError, "STORED_REPORT_UNAVAILABLE"):
            bridge.read_report(REPORT_ID)
        client.get_unified_report.assert_called_once_with(REPORT_ID)

    def test_stored_report_is_read_once_and_preserves_pending_queue(self):
        report = {"report_id": REPORT_ID, "as_of": NOW.isoformat(), "contract": "UNIFIED_RESEARCH_V1",
                  "candidates": [], "research_queue": [{"symbol": "005930", "action_decision": "VERIFY_PENDING"}],
                  "evaluations": [{"symbol": "005930", "total_score": 85, "action_decision": "VERIFY_PENDING"},
                                  {"symbol": "000660", "private_unrequested": "not forwarded"}]}
        reader = Mock(return_value=report)
        request = bridge.validate_request({"mode": "stored", "question": "검증 대기를 검토해줘",
                                           "report_id": REPORT_ID, "symbols": ["005930"]}, now=NOW)
        bundle, _ = bridge.build_bundle(request, report_reader=reader)
        reader.assert_called_once_with(REPORT_ID)
        self.assertEqual(bundle["stored_report"]["candidates"], [])
        self.assertEqual(bundle["stored_report"]["research_queue"][0]["action_decision"], "VERIFY_PENDING")
        self.assertEqual(len(bundle["stored_report"]["evaluations"]), 1)
        self.assertEqual(report["evaluations"][0]["total_score"], 85)

    def test_stored_review_cannot_mix_new_evidence_or_mismatch_report(self):
        request = {"mode": "stored", "question": "review", "report_id": REPORT_ID,
                   "symbols": ["005930"], "evidence": {"new": "data"}}
        with self.assertRaisesRegex(ValueError, "REPORT_ONLY"):
            bridge.validate_request(request, now=NOW)
        request.pop("evidence")
        request = bridge.validate_request(request, now=NOW)
        with self.assertRaisesRegex(ValueError, "REPORT_ID_MISMATCH"):
            bridge.build_bundle(request, report_reader=lambda _: {"report_id": "R-other"})
        with self.assertRaisesRegex(ValueError, "SYMBOL_NOT_IN"):
            bridge.build_bundle(request, report_reader=lambda _: {"report_id": REPORT_ID, "candidates": []})

    def test_bundle_size_is_bounded(self):
        request = research()
        request["evidence"]["sources"][0]["data"] = "가" * bridge.INPUT_LIMIT
        with self.assertRaisesRegex(ValueError, "TOO_LARGE"):
            bridge.build_bundle(bridge.validate_request(request, now=NOW))

    def test_large_realistic_lineage_and_pending_lists_are_bounded_with_exact_omission_counts(self):
        row = {"symbol": "005930", "action_decision": "VERIFY_PENDING", "financial_score": 85,
               "source_input_ids": ["I-" + str(i).zfill(64) for i in range(1800)],
               "pending_checks": ["MATERIAL_EVENT_REVIEW_REQUIRED:https://example.invalid/" + str(i) for i in range(450)]
                                 + ["RISK_HISTORY_COVERAGE_UNAVAILABLE"]}
        report = {"report_id": REPORT_ID, "candidates": [], "research_queue": [row], "evaluations": [row]}
        request = bridge.validate_request({"mode": "stored", "question": "검토해줘", "report_id": REPORT_ID,
                                           "symbols": ["005930"]}, now=NOW)
        bundle, _ = bridge.build_bundle(request, report_reader=lambda _: report)
        excerpt = bundle["stored_report"]["research_queue"][0]
        self.assertEqual(excerpt["source_input_ids_total"], 1800)
        self.assertEqual(excerpt["source_input_ids_omitted"], 1792)
        self.assertEqual(excerpt["pending_reason_counts"]["MATERIAL_EVENT_REVIEW_REQUIRED"], 450)
        self.assertEqual(excerpt["pending_reason_counts"]["RISK_HISTORY_COVERAGE_UNAVAILABLE"], 1)
        self.assertEqual(excerpt["action_decision"], "VERIFY_PENDING")
        self.assertEqual(len(row["source_input_ids"]), 1800)
        self.assertLess(len(json.dumps(bundle).encode()), bridge.INPUT_LIMIT)


class OrchestrationTests(unittest.TestCase):
    def test_three_real_profile_targets_are_started_concurrently_once_with_same_evidence(self):
        barrier = threading.Barrier(3, timeout=5)
        calls = []
        lock = threading.Lock()

        def runner(profile, prompt, directory):
            with lock:
                calls.append((profile, prompt.split("<evidence_json>\n", 1)[1], directory))
            barrier.wait()  # A sequential implementation fails, without a timing assertion.
            return {"profile": profile, "status": "COMPLETE", "memo": profile + " 실제 검토 fixture"}

        result = bridge.review(research(), runner=runner, now=NOW)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(sorted(c[0] for c in calls), sorted(bridge.PROFILES))
        self.assertEqual(len({c[1] for c in calls}), 1)
        self.assertFalse(calls[0][2].exists())  # Private temporary evidence is removed.
        self.assertFalse(result["orders_enabled"])
        self.assertFalse(result["stored_report_modified"])
        self.assertEqual(result["telegram_messages_sent"], 0)

    def test_partial_results_do_not_invent_missing_expert_or_retry(self):
        calls = []

        def runner(profile, prompt, directory):
            calls.append(profile)
            return {"profile": profile, "status": "UNAVAILABLE" if profile == "stock-risk" else "COMPLETE",
                    "memo": None if profile == "stock-risk" else "근거 있는 메모",
                    **({"error_code": "EXPERT_TIMEOUT"} if profile == "stock-risk" else {})}

        result = bridge.review(research(), runner=runner, now=NOW)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertCountEqual(calls, bridge.PROFILES)
        self.assertIsNone(result["specialists"][2]["memo"])
        self.assertEqual(result["specialists"][2]["error_code"], "EXPERT_TIMEOUT")

    def test_all_failures_remain_unavailable(self):
        result = bridge.review(research(), now=NOW, runner=lambda p, *_: {
            "profile": p, "status": "UNAVAILABLE", "memo": None})
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_one_unexpected_worker_failure_does_not_discard_completed_memos(self):
        def runner(profile, *_):
            if profile == "stock-risk":
                raise OSError("private fixture error")
            return {"profile": profile, "status": "COMPLETE", "memo": "확인된 메모"}
        result = bridge.review(research(), now=NOW, runner=runner)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["specialists"][0]["memo"], "확인된 메모")
        self.assertNotIn("private fixture error", json.dumps(result))


class ProcessTests(unittest.TestCase):
    def _run(self, output="검토 완료".encode("utf-8"), code=0, timeout=False):
        commands = []
        process = Mock()
        process.poll.return_value = 0
        process.wait.side_effect = subprocess.TimeoutExpired("hermes", bridge.CHILD_TIMEOUT) if timeout else None
        process.wait.return_value = code

        def spawn(command, **kwargs):
            commands.append((command, kwargs))
            kwargs["stdout"].write(output)
            kwargs["stderr"].write(b"secret diagnostic that must never be returned")
            return process

        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge.subprocess, "Popen", side_effect=spawn), \
                patch.object(bridge, "stop_process") as stop:
            result = bridge.run_specialist("stock-market", "untrusted $(command)", Path(temporary))
        return result, commands, stop

    def test_safe_argv_no_shell_fresh_session_no_tools_and_no_cio_token_inheritance(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/profiles/stock-cio",
                                     "STOCK_API_ROLE_TOKEN_FILE": "/private/cio-token"}):
            result, commands, _ = self._run()
        self.assertEqual(result["status"], "COMPLETE")
        command, kwargs = commands[0]
        self.assertEqual(command[:4], [bridge.HERMES, "-p", "stock-market", "chat"])
        self.assertEqual(command[command.index("--toolsets") + 1], "none")
        self.assertEqual(command[command.index("--source") + 1], "tool")
        self.assertNotIn("--resume", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("untrusted $(command)", command)
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("STOCK_API_ROLE_TOKEN_FILE", kwargs["env"])
        self.assertNotIn("HERMES_HOME", kwargs["env"])
        self.assertNotIn("secret diagnostic", json.dumps(result))

    def test_timeout_is_bounded_and_cleanup_is_requested(self):
        result, commands, stop = self._run(timeout=True)
        self.assertEqual(result["error_code"], "EXPERT_TIMEOUT")
        self.assertIsNone(result["memo"])
        self.assertTrue(stop.called)
        self.assertEqual(len(commands), 1)

    def test_optional_reasoning_and_budget_flags_are_validated_and_passed(self):
        commands = []
        process = Mock()
        process.wait.return_value = 0

        def spawn(command, **kwargs):
            commands.append(command)
            kwargs["stdout"].write("담당 결론: 확인 대기".encode())
            return process

        with tempfile.TemporaryDirectory() as temporary, patch.object(bridge.subprocess, "Popen", side_effect=spawn), \
                patch.object(bridge, "stop_process"):
            result = bridge.run_specialist(
                "stock-market", "prompt", Path(temporary), reasoning="medium", budget=120, timeout=140,
            )
            with self.assertRaises(ValueError):
                bridge.run_specialist("stock-market", "prompt", Path(temporary), reasoning="ultra")
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(commands[0][commands[0].index("--reasoning") + 1], "medium")
        self.assertEqual(commands[0][commands[0].index("--run-budget") + 1], "120")

    def test_only_known_empty_toolset_cli_line_is_removed(self):
        result, _, _ = self._run("Warning: Unknown toolsets: none\n담당 결론: 확인 대기".encode())
        self.assertEqual(result["memo"], "담당 결론: 확인 대기")
        result, _, _ = self._run(b"Error: private backend failure")
        self.assertEqual(result["error_code"], "EXPERT_UNEXPECTED_CLI_OUTPUT")
        self.assertIsNone(result["memo"])

    def test_failed_empty_oversized_or_invalid_output_is_not_a_memo(self):
        for raw, code, expected in ((b"", 0, "EXPERT_EMPTY_OR_INVALID_OUTPUT"),
                                    (b"text", 2, "EXPERT_PROCESS_FAILED"),
                                    (b"a" * (bridge.MEMO_LIMIT + 1), 0, "EXPERT_OUTPUT_TOO_LARGE"),
                                    (b"\xff", 0, "EXPERT_EXECUTION_UNAVAILABLE"),
                                    (b"\x1b[31mhello", 0, "EXPERT_EMPTY_OR_INVALID_OUTPUT")):
            with self.subTest(expected=expected):
                result, _, _ = self._run(raw, code)
                self.assertEqual(result["error_code"], expected)
                self.assertIsNone(result["memo"])


class StructuredToolTests(unittest.TestCase):
    def test_initialization_list_and_notifications_expose_only_fixed_review(self):
        initialized = bridge.dispatch_rpc({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        listed = bridge.dispatch_rpc({"id": 2, "method": "tools/list"})
        self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["stock_expert_review"])
        self.assertIsNone(bridge.dispatch_rpc({"method": "notifications/initialized"}))
        invalid = bridge.dispatch_rpc({"id": 3, "method": "tools/call", "params": "invalid"})
        self.assertEqual(invalid["error"]["code"], -32602)

    def test_native_call_passes_structured_data_once_and_does_not_run_a_shell(self):
        request = research()
        reviewer = Mock(return_value={"status": "COMPLETE", "specialists": [], "orders_enabled": False})
        response = bridge.dispatch_rpc({"id": 3, "method": "tools/call", "params": {
            "name": "stock_expert_review", "arguments": request}}, reviewer=reviewer)
        reviewer.assert_called_once_with(request)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"])["status"], "COMPLETE")
        unknown = bridge.dispatch_rpc({"id": 4, "method": "tools/call", "params": {"name": "execute_code"}}, reviewer=reviewer)
        self.assertIn("error", unknown)
        self.assertEqual(reviewer.call_count, 1)

    def test_failed_native_validation_keeps_details_private_and_no_fabricated_memos(self):
        response = bridge.dispatch_rpc({"id": 1, "method": "tools/call", "params": {
            "name": "stock_expert_review", "arguments": {}}}, reviewer=Mock(side_effect=ValueError("INVALID_REVIEW_MODE")))
        self.assertTrue(response["result"]["isError"])
        data = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(data["error_code"], "INVALID_REVIEW_MODE")
        self.assertEqual(data["specialists"], [])

    def test_stdio_has_only_protocol_json_and_ignores_notifications(self):
        source = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
                            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                            b'invalid json\n')
        output = io.StringIO()
        bridge.serve_mcp(source, output)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
