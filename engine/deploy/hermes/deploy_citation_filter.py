"""Approved host-side rollout of the stock-cio Telegram citation boundary.

Read an authenticated operator payload on stdin. Never accepts arbitrary target paths,
credentials, message text or database writes. Backups remain on the operating host.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


RUNTIME = Path("/home/ubuntu/.hermes/hermes-agent")
PROFILE = Path("/home/ubuntu/.hermes/profiles/stock-cio")
UNIT = "hermes-gateway-stock-cio.service"
GATEWAY = RUNTIME / "gateway/run.py"
HELPER = RUNTIME / "gateway/stock_cio_citations.py"
BACKUP_ROOT = Path("/home/ubuntu/.hermes/backups/telegram-citations")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw, mode):
    if path.resolve() != path:
        raise ValueError("UNEXPECTED_TARGET")
    fd, temporary = tempfile.mkstemp(prefix=".citation-filter-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service():
    raw = subprocess.check_output(["systemctl", "--user", "show", UNIT,
                                   "--property=ActiveState,SubState,NRestarts,ExecMainStatus"], text=True, timeout=10)
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def rollout(request):
    if request.get("approval") != "DEPLOY_STOCK_CIO_TELEGRAM_CITATION_FILTER":
        raise ValueError("EXPLICIT_DEPLOYMENT_APPROVAL_REQUIRED")
    if GATEWAY.resolve() != GATEWAY or HELPER.resolve() != HELPER:
        raise ValueError("UNEXPECTED_TARGET")
    before = GATEWAY.read_bytes()
    if digest(before) != request["gateway_before_sha256"]:
        raise ValueError("GATEWAY_SOURCE_CHANGED")
    if HELPER.exists():
        raise ValueError("HELPER_ALREADY_EXISTS")
    helper = base64.b64decode(request["helper_base64"], validate=True)
    patcher = base64.b64decode(request["patcher_base64"], validate=True)
    if digest(helper) != request["helper_sha256"] or digest(patcher) != request["patcher_sha256"]:
        raise ValueError("PAYLOAD_HASH_MISMATCH")
    namespace = {}
    exec(compile(patcher, "citation_patch.py", "exec"), namespace)
    after = namespace["patch_gateway"](before.decode("utf-8")).encode("utf-8")
    compile(helper, str(HELPER), "exec")
    compile(after, str(GATEWAY), "exec")
    preserved = {name: digest((PROFILE / name).read_bytes()) for name in
                 ("config.yaml", "tool-policy.json", "SOUL.md", "skills/stock-research/SKILL.md",
                  "skills/stock-research/scripts/read_dart.py", "scripts/stock-live-market-query.sh")}
    if service()["ActiveState"] != "active":
        raise ValueError("SERVICE_NOT_ACTIVE")
    backup = BACKUP_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, mode=0o700)
    saved = backup / "gateway-run.py"
    saved.write_bytes(before)
    saved.chmod(0o600)
    if digest(saved.read_bytes()) != digest(before):
        raise ValueError("BACKUP_MISMATCH")
    mode = GATEWAY.stat().st_mode & 0o777
    helper_created = gateway_written = False
    try:
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=True, timeout=30)
        if service()["ActiveState"] != "inactive":
            raise ValueError("SERVICE_NOT_STOPPED")
        if GATEWAY.read_bytes() != before or HELPER.exists():
            raise ValueError("SOURCE_CHANGED_DURING_STOP")
        atomic(HELPER, helper, 0o644)
        helper_created = True
        atomic(GATEWAY, after, mode)
        gateway_written = True
        # Exercise the installed helper without starting a gateway, model or Telegram client.
        spec = importlib.util.spec_from_file_location("deployed_citations", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        os.environ["HERMES_HOME"] = str(PROFILE)
        sample = "근거 \ue200cite\ue202turn5search0\ue201\n[원문](https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260814000604)"
        if module.sanitize_stock_cio_citations("telegram", sample) != sample.replace(" \ue200cite\ue202turn5search0\ue201", ""):
            raise ValueError("INSTALLED_FILTER_FAILED")
        if module.sanitize_stock_cio_citations("local", sample) != sample:
            raise ValueError("PLATFORM_SCOPE_CHANGED")
        os.environ["HERMES_HOME"] = "/home/ubuntu/.hermes/profiles/stock-risk"
        if module.sanitize_stock_cio_citations("telegram", sample) != sample:
            raise ValueError("PROFILE_SCOPE_CHANGED")
        subprocess.run(["systemctl", "--user", "start", UNIT], check=True, timeout=30)
        final = service()
        if final["ActiveState"] != "active" or final["SubState"] != "running":
            raise ValueError("SERVICE_NOT_RUNNING")
        with urllib.request.urlopen("http://127.0.0.1:9120/health", timeout=5) as response:
            health = json.load(response)
        if health["status"] != "ok" or health["orders_enabled"] is not False:
            raise ValueError("HEALTH_FAILED")
        if any(digest((PROFILE / name).read_bytes()) != wanted for name, wanted in preserved.items()):
            raise ValueError("UNRELATED_PROFILE_FILE_CHANGED")
    except Exception:
        try:
            if gateway_written:
                if GATEWAY.read_bytes() != after:
                    raise ValueError("ROLLBACK_SOURCE_CHANGED")
                atomic(GATEWAY, before, mode)
            if helper_created and HELPER.exists() and digest(HELPER.read_bytes()) == digest(helper):
                HELPER.unlink()
        finally:
            subprocess.run(["systemctl", "--user", "start", UNIT], timeout=30)
        raise
    record = {"deployed": True, "backup": str(backup), "gateway_before_sha256": digest(before),
              "gateway_after_sha256": digest(after), "helper_sha256": digest(helper),
              "exact_insertion_verified": after.replace(namespace["INSERT"].encode(), b"", 1) == before,
              "service": final, "orders_enabled": False, "profile_files_preserved": True,
              "database_writes": 0, "prompt_cache_writes": 0, "telegram_messages_sent": 0,
              "client_display_verified": False, "scope": "stock-cio Telegram final responses only"}
    (backup / "manifest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (backup / "manifest.json").chmod(0o600)
    return record


if __name__ == "__main__":
    print(json.dumps(rollout(json.load(sys.stdin))))
