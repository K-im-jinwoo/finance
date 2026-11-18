"""Run the host shell with fake Docker/renderer; never call external services."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy/jobs/run_stock_cycle.sh"
GIT_SH = Path("C:/Program Files/Git/bin/bash.exe")
SHELL = str(GIT_SH) if os.name == "nt" and GIT_SH.exists() else (shutil.which("sh") if os.name != "nt" else None)
REPORT_ID = "UR-" + "a" * 24
REPORT = f"통합 종목 연구\n보고서: {REPORT_ID}\n후보 픽스처"


@unittest.skipUnless(SHELL, "POSIX shell is unavailable")
class CandidateChartCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "chart-calls.txt"
        self.envfile = self.root / "candidate.env"
        self.envfile.write_text("# offline fixture\n", encoding="utf-8")
        docker = self.bin / "docker"
        docker.write_text("""#!/bin/sh
case "$*" in
  *refresh-daily*) exit "${FAKE_DAILY_EXIT:-0}" ;;
  *refresh-unified*) printf '%s\\n' "$FAKE_REPORT" ;;
  *) exit 33 ;;
esac
""", encoding="utf-8", newline="\n")
        renderer = self.bin / "chart-python"
        renderer.write_text("""#!/bin/sh
printf '%s\\n' "$*" > "$CHART_TEST_CALLS"
if [ "${FAKE_CHART_EXIT:-0}" != 0 ]; then exit "$FAKE_CHART_EXIT"; fi
printf 'MEDIA:/fixture/candidate.png\\n'
""", encoding="utf-8", newline="\n")
        docker.chmod(0o755)
        renderer.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "STOCK_CANDIDATE_ENV_FILE": self.envfile.as_posix(), "STOCK_UNIFIED_ENABLED": "true",
                    "STOCK_NEWS_ENABLED": "false", "STOCK_CHART_PYTHON": renderer.as_posix(),
                    "CHART_TEST_CALLS": self.calls.as_posix(), "FAKE_REPORT": REPORT,
                    "STOCK_CANDIDATE_CHARTS": "false"}

    def run_cycle(self, **env):
        return subprocess.run([SHELL, SCRIPT.as_posix(), "morning"], env={**self.env, **env},
                              capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_opt_in_off_preserves_the_existing_report_and_does_not_call_renderer(self):
        result = self.run_cycle()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), REPORT)
        self.assertFalse(self.calls.exists())

    def test_enabled_passes_the_exact_generated_report_id_to_the_host_renderer(self):
        result = self.run_cycle(STOCK_CANDIDATE_CHARTS="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(REPORT, result.stdout)
        self.assertIn("MEDIA:/fixture/candidate.png", result.stdout)
        self.assertIn("--report-id " + REPORT_ID, self.calls.read_text(encoding="utf-8"))

    def test_chart_failure_does_not_discard_the_report_or_claim_an_attachment(self):
        result = self.run_cycle(STOCK_CANDIDATE_CHARTS="true", FAKE_CHART_EXIT="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(REPORT, result.stdout)
        self.assertIn("기술적 차트 첨부 실패", result.stdout)
        self.assertNotIn("MEDIA:", result.stdout)

    def test_failed_price_refresh_cannot_attach_a_chart_from_an_older_report(self):
        result = self.run_cycle(STOCK_CANDIDATE_CHARTS="true", FAKE_DAILY_EXIT="9")
        self.assertEqual(result.returncode, 9)
        self.assertNotIn("MEDIA:", result.stdout)
        self.assertIn("stage=DAILY_REFRESH", result.stderr)
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
