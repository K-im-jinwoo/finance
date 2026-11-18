"""Hash-guarded rollout of the approved Telegram specialist bridge only.

Run on the Oracle host as root with an operator payload on stdin. Does not
modify runtime code, credentials, schedules, engine data, messages or DB caches.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import urllib.request


PROFILE_ROOT = Path("/home/ubuntu/.hermes/profiles")
BACKUP_ROOT = Path("/home/ubuntu/.hermes/backups/telegram-expert-review")
UNIT = "hermes-gateway-stock-cio.service"
RELATIVE_FILES = (
    "stock-cio/SOUL.md", "stock-cio/skills/stock-research/SKILL.md", "stock-cio/tool-policy.json",
    "stock-cio/scripts/stock-expert-review.py", "stock-market/SOUL.md",
    "stock-fundamentals/SOUL.md", "stock-risk/SOUL.md",
    "stock-cio/skills/stock-research/investment-committee-review/SKILL.md",
)
PRESERVED_FILES = (
    "stock-cio/profile.env", "stock-cio/.env",
    "stock-cio/scripts/stock-live-market-query.sh", "stock-cio/cron/jobs.json",
    "stock-live-market/SOUL.md", "stock-live-market/scripts/stock-intraday.sh",
    "stock-market/config.yaml", "stock-fundamentals/config.yaml", "stock-risk/config.yaml",
)
MCP_ENTRY = {"command": "python3", "args": [str(PROFILE_ROOT / "stock-cio/scripts/stock-expert-review.py"), "--mcp"],
             "timeout": 100, "connect_timeout": 10}


def read_yaml(raw):
    import yaml
    return yaml.safe_load(raw)


def add_mcp_config(raw):
    # YAML is already a dependency of the installed Hermes runtime. No new
    # package is installed. Preserve every existing byte and semantic field.
    before = read_yaml(raw)
    if not isinstance(before, dict):
        raise ValueError("INVALID_CIO_CONFIG")
    servers = before.get("mcp_servers")
    if servers is not None:
        if isinstance(servers, dict) and servers.get("stock_experts") == MCP_ENTRY:
            return raw
        raise ValueError("EXISTING_MCP_CONFIG_REQUIRES_REVIEW")
    if "mcp_servers" in before:
        raise ValueError("EXISTING_MCP_CONFIG_REQUIRES_REVIEW")
    block = "\nmcp_servers:\n  stock_experts:\n    command: python3\n    args:\n"
    for argument in MCP_ENTRY["args"]:
        block += "      - " + json.dumps(argument) + "\n"
    block += "    timeout: 100\n    connect_timeout: 10\n"
    after = raw + (b"" if raw.endswith(b"\n") else b"\n") + block.encode()
    expected = dict(before)
    expected["mcp_servers"] = {"stock_experts": MCP_ENTRY}
    if read_yaml(after) != expected:
        raise ValueError("UNRELATED_CIO_CONFIG_CHANGE")
    return after


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def state(path):
    return digest(path.read_bytes()) if path.is_file() else None


def host_owner():
    import pwd
    return pwd.getpwnam("ubuntu")


def service(action=None):
    user = host_owner()
    command = ["sudo", "-n", "-u", "ubuntu", "env", f"XDG_RUNTIME_DIR=/run/user/{user.pw_uid}",
               "systemctl", "--user"]
    if action:
        subprocess.run(command + [action, UNIT], check=True, timeout=30)
    raw = subprocess.check_output(command + ["show", UNIT,
                  "--property=ActiveState,SubState,NRestarts,ExecMainStatus"], text=True, timeout=10)
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def health():
    with urllib.request.urlopen("http://127.0.0.1:9120/health", timeout=5) as response:
        value = json.load(response)
    if value.get("status") != "ok" or value.get("orders_enabled") is not False:
        raise ValueError("STOCK_HEALTH_OR_ORDER_BOUNDARY_FAILED")
    return {"status": value["status"], "orders_enabled": False}


def telegram_state():
    """Minimum metadata only; never clear or replace a user's cached prompt."""
    path = PROFILE_ROOT / "stock-cio/state.db"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT id,system_prompt,system_prompt_hash FROM sessions WHERE source='telegram'").fetchall()
        if len(rows) != 1:
            raise ValueError("TELEGRAM_SESSION_MISSING_OR_AMBIGUOUS")
        identifier, prompt, prompt_hash = rows[0]
        if prompt:
            raise ValueError("EXISTING_PROMPT_CACHE_REQUIRES_SEPARATE_HANDLING")
        row = connection.execute("SELECT role,id,tool_calls,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
                                 (identifier,)).fetchone()
        if row is None or row[0] != "assistant" or row[2] or not row[3]:
            raise ValueError("TELEGRAM_CONVERSATION_BUSY")
        count = connection.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (identifier,)).fetchone()[0]
        return {"session_id": identifier, "latest_message_id": row[1], "messages": count,
                "prompt_cache_empty": True,
                # Hermes rebuilds a NULL prompt even if an old hash remains.
                # Preserve that metadata byte-for-byte; do not clear the DB.
                "orphan_prompt_hash": prompt_hash}
    finally:
        connection.close()


def validate_payload(request):
    if request.get("approval") != "CONNECT_STOCK_EXPERTS_TO_TELEGRAM":
        raise ValueError("EXPLICIT_CONNECTION_APPROVAL_REQUIRED")
    files = request.get("files")
    if not isinstance(files, dict) or set(files) != set(RELATIVE_FILES):
        raise ValueError("UNEXPECTED_FILE_SET")
    decoded = {}
    for relative, entry in files.items():
        target = PROFILE_ROOT / relative
        if target.resolve() != target or not target.parent.is_dir():
            raise ValueError("UNEXPECTED_TARGET")
        if state(target) != entry.get("before_sha256"):
            raise ValueError("PROFILE_SOURCE_CHANGED")
        raw = base64.b64decode(entry["base64"], validate=True)
        if not raw or len(raw) > 250_000 or digest(raw) != entry["sha256"]:
            raise ValueError("PAYLOAD_HASH_MISMATCH")
        text = raw.decode("utf-8")
        if relative.endswith(".py"):
            compile(text, relative, "exec")
        elif relative.endswith(".json"):
            policy = json.loads(text)
            if policy.get("role") != "cio" or any("/orders" in item for item in policy.get("allowed", [])):
                raise ValueError("ORDER_TOOL_FORBIDDEN")
        elif "STOCK_EXPERT_REVIEW_V1" not in text and "TELEGRAM_EXPERT_REVIEW_V1" not in text:
            raise ValueError("MISSING_REVIEW_GUIDANCE")
        decoded[relative] = raw
    config = PROFILE_ROOT / "stock-cio/config.yaml"
    if config.resolve() != config or state(config) != request.get("cio_config_before_sha256"):
        raise ValueError("CIO_CONFIG_SOURCE_CHANGED")
    decoded["stock-cio/config.yaml"] = add_mcp_config(config.read_bytes())
    return decoded


def atomic(path, raw, mode, uid, gid):
    if path.resolve() != path:
        raise ValueError("UNEXPECTED_TARGET")
    fd, temporary = tempfile.mkstemp(prefix=".stock-review-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rollout(request):
    from datetime import datetime, timezone
    decoded = validate_payload(request)
    initial = service()
    if initial.get("ActiveState") != "active" or initial.get("SubState") != "running":
        raise ValueError("CIO_SERVICE_NOT_ACTIVE")
    health()
    conversation = telegram_state()
    preserved = {name: state(PROFILE_ROOT / name) for name in PRESERVED_FILES}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = BACKUP_ROOT / stamp
    backup.mkdir(parents=True, mode=0o700)
    before = {}
    owner = host_owner()
    for relative in decoded:
        target = PROFILE_ROOT / relative
        raw = target.read_bytes() if target.is_file() else None
        metadata = target.stat() if target.exists() else None
        before[relative] = {"raw": raw, "mode": metadata.st_mode & 0o777 if metadata else 0o644,
                            "uid": metadata.st_uid if metadata else owner.pw_uid,
                            "gid": metadata.st_gid if metadata else owner.pw_gid}
        if raw is not None:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            saved.write_bytes(raw)
            saved.chmod(0o600)
            if state(saved) != digest(raw):
                raise ValueError("BACKUP_HASH_MISMATCH")
    manifest = {name: {"before_sha256": digest(before[name]["raw"]) if before[name]["raw"] is not None else None,
                       "after_sha256": digest(raw)} for name, raw in decoded.items()}
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    changed = []
    try:
        if telegram_state() != conversation:
            raise ValueError("TELEGRAM_CHANGED_DURING_PREFLIGHT")
        if service("stop").get("ActiveState") != "inactive":
            raise ValueError("CIO_SERVICE_NOT_STOPPED")
        if telegram_state() != conversation:
            raise ValueError("TELEGRAM_CHANGED_DURING_STOP")
        validate_payload(request)
        for relative, raw in decoded.items():
            target = PROFILE_ROOT / relative
            entry = before[relative]
            atomic(target, raw, entry["mode"], entry["uid"], entry["gid"])
            changed.append(relative)
        final = service("start")
        for _ in range(10):
            if final.get("ActiveState") == "active" and final.get("SubState") == "running":
                break
            time.sleep(0.5)
            final = service()
        else:
            raise ValueError("CIO_SERVICE_NOT_RUNNING")
        health()
        if any(state(PROFILE_ROOT / name) != expected for name, expected in preserved.items()):
            raise ValueError("UNRELATED_CONFIGURATION_CHANGED")
        if telegram_state() != conversation:
            raise ValueError("TELEGRAM_HISTORY_CHANGED_DURING_DEPLOY")
    except Exception:
        service("stop")
        for relative in reversed(changed):
            target = PROFILE_ROOT / relative
            entry = before[relative]
            if state(target) != digest(decoded[relative]):
                raise ValueError("ROLLBACK_TARGET_CHANGED")
            if entry["raw"] is None:
                target.unlink()
            else:
                atomic(target, entry["raw"], entry["mode"], entry["uid"], entry["gid"])
        service("start")
        raise
    result = {"deployed": True, "backup": str(backup), "files": manifest,
              "service": final, "health": health(), "unrelated_configuration_preserved": True,
              "telegram_history_metadata_preserved": True, "database_writes": 0,
              "prompt_cache_writes": 0, "telegram_messages_sent": 0}
    (backup / "deployment.json").write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    import sys
    print(json.dumps(rollout(json.load(sys.stdin)), ensure_ascii=False))
