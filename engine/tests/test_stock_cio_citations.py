import importlib.util
import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1] / "hermes/runtime"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


citations = load("stock_cio_citations")
installer = load("citation_patch")
CITE = "\ue200cite\ue202turn5search0\ue201"
URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260814000604"
FIXTURE = '''# Preserve upstream imports and helpers.
def untouched():
    return "same"

def _sanitize_gateway_final_response(platform, text):
    if not text:
        return text
    text = _sanitize_surrogates(str(text))
    return redact(text)

AFTER = "same"
'''


class CitationTests(unittest.TestCase):
    def test_stale_single_and_multiple_refs_disappear_but_facts_and_links_survive(self):
        multi = "\ue200cite\ue202turn5search1\ue202turn5search3\ue201"
        raw = f"별도 OCF 전년 989.7억 → 당기 691.1억. {CITE}\n보유 {multi}\n[반기 원문]({URL})"
        expected = f"별도 OCF 전년 989.7억 → 당기 691.1억.\n보유\n[반기 원문]({URL})"
        self.assertEqual(citations.remove_opaque_citations(raw), expected)

    def test_real_markdown_urls_and_unmarked_content_are_byte_identical(self):
        raw = f"- [공식 원문]({URL})\r\n- https://dart.fss.or.kr/report/viewer.do?a=1&b=2\r\n[1] cite turn5search0."
        self.assertEqual(citations.remove_opaque_citations(raw), raw)

    def test_inline_and_fenced_code_examples_are_preserved(self):
        for example in (f"`{CITE}`", f"``{CITE}``", f"```python\n{CITE}\n```", f"~~~text\n{CITE}\n~~~"):
            with self.subTest(example=example):
                self.assertEqual(citations.remove_opaque_citations(example + "\n문장 " + CITE), example + "\n문장")

    def test_unclosed_fenced_example_is_preserved(self):
        raw = "```text\nexample " + CITE
        self.assertEqual(citations.remove_opaque_citations(raw), raw)

    def test_longer_closing_fence_does_not_hide_citations_in_following_prose(self):
        for fence in ("`", "~"):
            example = fence * 3 + "text\n" + CITE + "\n" + fence * 4
            self.assertEqual(citations.remove_opaque_citations(example + "\n문장 " + CITE), example + "\n문장")

    def test_truncated_metadata_at_line_end_does_not_swallow_following_prose(self):
        raw = "검토 \ue200cite\ue202turn7search2\n다음 문장"
        self.assertEqual(citations.remove_opaque_citations(raw), "검토\n다음 문장")
        raw = "설명 \ue200cite\ue202turn7search2 실제 본문"
        self.assertEqual(citations.remove_opaque_citations(raw), raw)

    def test_empty_input_and_repeated_application(self):
        self.assertEqual(citations.remove_opaque_citations(""), "")
        self.assertEqual(citations.remove_opaque_citations(CITE), "")
        once = citations.remove_opaque_citations("검토 " + CITE)
        self.assertEqual(citations.remove_opaque_citations(once), once)

    def test_only_stock_cio_telegram_is_changed(self):
        class Platform(Enum):
            TELEGRAM = "telegram"
        for home, platform, changed in (("/profiles/stock-cio", Platform.TELEGRAM, True),
                                         ("/profiles/stock-risk", "telegram", False),
                                         ("/profiles/stock-cio", "local", False),
                                         ("/profiles/stock-cio", "api", False),
                                         ("", "telegram", False)):
            with self.subTest(home=home, platform=platform), patch.dict(os.environ, {"HERMES_HOME": home}):
                self.assertEqual(citations.sanitize_stock_cio_citations(platform, "내용 " + CITE), "내용" if changed else "내용 " + CITE)

    def test_patch_is_exact_insertion_and_idempotent(self):
        result = installer.patch_gateway(FIXTURE)
        self.assertEqual(result.replace(installer.INSERT, "", 1), FIXTURE)
        self.assertEqual(installer.patch_gateway(result), result)

    def test_upstream_drift_is_rejected_without_mutating_input(self):
        for source in (FIXTURE.replace(installer.ANCHOR, "    text = str(text)\n"),
                       FIXTURE.replace("_sanitize_gateway_final_response", "renamed"),
                       FIXTURE + "# " + installer.MARKER):
            with self.subTest(source=source), self.assertRaises(ValueError):
                installer.patch_gateway(source)

    def test_hook_executes_before_existing_redaction_and_preserves_other_functions(self):
        import sys
        from types import ModuleType
        package = ModuleType("gateway")
        module = ModuleType("gateway.stock_cio_citations")
        module.sanitize_stock_cio_citations = citations.sanitize_stock_cio_citations
        namespace = {"_sanitize_surrogates": str, "redact": lambda s: s.replace("private-key", "[redacted]")}
        with patch.dict(sys.modules, {"gateway": package, "gateway.stock_cio_citations": module}), patch.dict(os.environ, {"HERMES_HOME": "/profiles/stock-cio"}):
            exec(installer.patch_gateway(FIXTURE), namespace)
            self.assertEqual(namespace["_sanitize_gateway_final_response"]("telegram", f"private-key {CITE}\n[원문]({URL})"), f"[redacted]\n[원문]({URL})")
            self.assertEqual(namespace["untouched"](), "same")
            self.assertEqual(namespace["AFTER"], "same")


class RolloutTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("deploy_citation_filter", ROOT.parents[1] / "deploy/hermes/deploy_citation_filter.py")
        self.deployer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.deployer)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.runtime = base / "runtime"
        self.profile = base / "profiles/stock-cio"
        self.gateway = self.runtime / "gateway/run.py"
        self.helper = self.gateway.with_name("stock_cio_citations.py")
        self.gateway.parent.mkdir(parents=True)
        self.gateway.write_bytes(FIXTURE.encode())
        for name in ("config.yaml", "tool-policy.json", "SOUL.md", "skills/stock-research/SKILL.md",
                     "skills/stock-research/scripts/read_dart.py", "scripts/stock-live-market-query.sh"):
            path = self.profile / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserved", encoding="utf-8")
        patcher = patch.multiple(self.deployer, RUNTIME=self.runtime, PROFILE=self.profile,
                                 GATEWAY=self.gateway, HELPER=self.helper, BACKUP_ROOT=base / "backups")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        helper = (ROOT / "stock_cio_citations.py").read_bytes()
        installer_source = (ROOT / "citation_patch.py").read_bytes()
        self.request = {"approval": "DEPLOY_STOCK_CIO_TELEGRAM_CITATION_FILTER",
                        "gateway_before_sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(),
                        "helper_base64": base64.b64encode(helper).decode(),
                        "helper_sha256": hashlib.sha256(helper).hexdigest(),
                        "patcher_base64": base64.b64encode(installer_source).decode(),
                        "patcher_sha256": hashlib.sha256(installer_source).hexdigest()}

    def run_rollout(self, *, health_ok=True, on_command=None):
        response = MagicMock()
        response.__enter__.return_value = io.StringIO(json.dumps({"status": "ok" if health_ok else "error", "orders_enabled": False}))
        active = {"ActiveState": "active", "SubState": "running", "NRestarts": "0", "ExecMainStatus": "0"}
        inactive = {**active, "ActiveState": "inactive", "SubState": "dead"}
        with patch.object(self.deployer, "service", side_effect=[active, inactive, active]), \
                patch.object(self.deployer.subprocess, "run", side_effect=on_command) as commands, \
                patch.object(self.deployer.urllib.request, "urlopen", return_value=response):
            result = self.deployer.rollout(self.request)
        return result, commands

    def test_rollout_backs_up_and_changes_only_the_guarded_boundary(self):
        result, commands = self.run_rollout()
        self.assertTrue(result["exact_insertion_verified"])
        self.assertEqual((Path(result["backup"]) / "gateway-run.py").read_bytes(), FIXTURE.encode())
        self.assertEqual(self.gateway.read_text().replace(installer.INSERT, "", 1), FIXTURE)
        self.assertTrue(self.helper.exists())
        self.assertEqual([c.args[0][2:4] for c in commands.call_args_list], [["stop", self.deployer.UNIT], ["start", self.deployer.UNIT]])
        self.assertEqual(result["database_writes"], 0)
        self.assertEqual((self.profile / "config.yaml").read_text(), "preserved")

    def test_health_failure_restores_code_and_restarts_service(self):
        with self.assertRaisesRegex(ValueError, "HEALTH_FAILED"):
            self.run_rollout(health_ok=False)
        self.assertEqual(self.gateway.read_bytes(), FIXTURE.encode())
        self.assertFalse(self.helper.exists())

    def test_source_changed_during_stop_is_preserved(self):
        def command(args, **kwargs):
            if "stop" in args:
                self.gateway.write_text("# external update\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED_DURING_STOP"):
            self.run_rollout(on_command=command)
        self.assertEqual(self.gateway.read_text(), "# external update\n")
        self.assertFalse(self.helper.exists())

    def test_hash_drift_is_rejected_before_service_or_file_writes(self):
        self.request["gateway_before_sha256"] = "0" * 64
        with patch.object(self.deployer.subprocess, "run") as command, self.assertRaisesRegex(ValueError, "GATEWAY_SOURCE_CHANGED"):
            self.deployer.rollout(self.request)
        command.assert_not_called()
        self.assertFalse(self.helper.exists())


if __name__ == "__main__":
    unittest.main()
