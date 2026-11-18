"""Execute the bounded, read-only report audit over existing SSH trust.

No installation, credentials, account tables, providers or messages are accessed.
"""
import argparse
import base64
import json
import shlex
import subprocess
from bootstrap import ROOT


def remote_code(reports: int) -> str:
    if isinstance(reports, bool) or not 1 <= reports <= 20:
        raise ValueError("reports must be between 1 and 20")
    source = ROOT / "engine/src/stock_assistant"
    reader = (source / "research_repository.py").read_text(encoding="utf-8").replace(
        "from .repository import", "from stock_assistant.repository import")
    audit = (source / "screening_diagnostics.py").read_text(encoding="utf-8").replace(
        "from .research_repository import ReadOnlyResearchRepository", "").replace(
        "from .validation import", "from stock_assistant.validation import")
    # Execute separate compilation units so future imports retain their required position.
    return (f"exec({reader!r});exec({audit!r});"
            "print(__import__('json').dumps(audit_screening(ReadOnlyResearchRepository("
            "__import__('pathlib').Path('/var/lib/stock/stock-assistant.sqlite3')),"
            "as_of=__import__('datetime').datetime.now(__import__('datetime').timezone.utc),"
            f"report_limit={reports}),ensure_ascii=False))")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="existing trusted SSH host; no trust configuration changes")
    parser.add_argument("--reports", type=int, default=8)
    args = parser.parse_args(argv)
    if args.host.startswith("-") or any(character.isspace() for character in args.host):
        parser.error("host must be an SSH host without options or whitespace")
    encoded = base64.b64encode(remote_code(args.reports).encode()).decode("ascii")
    command = "docker exec stock-assistant-stock-assistant-1 python -c " + shlex.quote(
        "import base64;exec(base64.b64decode('" + encoded + "'))")
    process = subprocess.run(["C:/WINDOWS/System32/OpenSSH/ssh.exe", "-T", "-o", "BatchMode=yes",
                              "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", args.host, command],
                             capture_output=True, encoding="utf-8", timeout=45,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise RuntimeError("READ_ONLY_SCREENING_AUDIT_FAILED")
    if len(process.stdout) > 200_000:
        raise ValueError("audit response exceeded the output bound")
    result = json.loads(process.stdout)
    if not result.get("read_only"):
        raise ValueError("audit did not confirm read-only access")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
