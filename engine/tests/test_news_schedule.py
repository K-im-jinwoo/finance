"""Exercise the actual shell route using a fake Docker executable, without services."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")


@unittest.skipUnless(BASH, "bash is required for the shell route fixture")
class NewsScheduleTests(unittest.TestCase):
    def route(self, enabled="false", fail=False, mode="morning"):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "candidate.env"
            config.write_text("# fixture paths only\n", encoding="utf-8")
            log = root / "calls.txt"
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$STOCK_NEWS_TEST_LOG\"\n"
                              "case \"$*\" in *discover-news*) [ \"$STOCK_NEWS_TEST_FAIL\" != true ] ;; *) exit 0 ;; esac\n",
                              encoding="utf-8")
            docker.chmod(0o700)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "STOCK_NEWS_ENABLED": enabled, "STOCK_DART_BUSINESS_YEAR": "2025",
                   "STOCK_CANDIDATE_ENV_FILE": config.as_posix(),
                   "STOCK_NEWS_TEST_LOG": log.as_posix(), "STOCK_NEWS_TEST_FAIL": str(fail).lower()}
            call = subprocess.run([BASH, (ROOT / "deploy/jobs/run_stock_cycle.sh").as_posix(), mode],
                                  env=env, capture_output=True, text=True, timeout=20,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return call, calls

    def test_disabled_keeps_existing_route_and_does_not_load_news_secrets(self):
        result, calls = self.route()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("discover-news" in line or "compose.news" in line or "--include-news" in line for line in calls))
        self.assertEqual(len(calls), 2)

    def test_enabled_discovers_before_due_diligence_then_includes_news_in_report(self):
        result, calls = self.route(enabled="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 4)
        self.assertIn("discover-news", calls[0])
        self.assertIn("compose.news.yaml", calls[0])
        self.assertIn("enrich-dart ", calls[1])
        self.assertIn("enrich-dart-disclosures", calls[2])
        self.assertTrue(all("--include-news" in line for line in calls[1:]))
        self.assertIn("generate-candidates", calls[3])

    def test_failed_fetch_keeps_base_report_and_marks_news_unavailable(self):
        result, calls = self.route(enabled="true", fail=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable", result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("--include-news", calls[1])
        self.assertIn("--news-fetch-failed", calls[-1])

    def test_invalid_flag_fails_before_any_service_command(self):
        result, calls = self.route(enabled="yes")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
