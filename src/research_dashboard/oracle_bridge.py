"""Bounded JSON line reads over existing SSH trust; no remote installation."""
from __future__ import annotations

import base64
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from .hermes_reports import parse_report
from .live_accounts import utc_now


class SSHReader:
    def __init__(self, script: Path, container=False):
        self.script, self.container = script, container
        self.process = None
        self.lines = queue.Queue(maxsize=8)

    def _start(self):
        encoded = base64.b64encode(self.script.read_bytes()).decode('ascii')
        code = "import base64;exec(base64.b64decode(\""+encoded+"\"))"
        command = ("docker exec -i stock-assistant-stock-assistant-1 python -u -c '" if self.container else "python3 -u -c '")+code+"'"
        args = ["C:/WINDOWS/System32/OpenSSH/ssh.exe","-T","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes",
                "-o","ConnectTimeout=10","-o","ServerAliveInterval=20","-o","ServerAliveCountMax=2","144.24.92.159",command]
        self.lines = queue.Queue(maxsize=8)
        self.process = subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                        text=True,encoding="utf-8",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        lines, process = self.lines, self.process
        def read():
            try:
                while True:
                    line = process.stdout.readline(1_200_001)
                    if not line:
                        lines.put(None,timeout=1)
                        return
                    if len(line) > 1_200_000:
                        lines.put(None,timeout=1)
                        return
                    lines.put(line,timeout=1)
            except (ValueError,OSError,queue.Full):
                pass
        threading.Thread(target=read,daemon=True).start()

    def request(self, body):
        if self.process is None or self.process.poll() is not None:
            self._start()
        try:
            self.process.stdin.write(json.dumps(body)+"\n")
            self.process.stdin.flush()
            line = self.lines.get(timeout=25)
            if line is None:
                raise ConnectionError("SSH_READER_CLOSED")
            result = json.loads(line)
            if not isinstance(result,dict):
                raise ValueError("invalid reader response")
            return result
        except (OSError,ValueError,queue.Empty,ConnectionError):
            self.close()
            raise ConnectionError("READ_ONLY_SOURCE_UNAVAILABLE") from None

    def close(self):
        if self.process is not None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=2)
            self.process = None


class HermesLiveWorker:
    def __init__(self, root, accounts, stop, direct=False, allow_open_fills=False):
        self.accounts, self.stop, self.direct, self.allow_open_fills = accounts, stop, direct, allow_open_fills
        self.reports = SSHReader(root/"scripts/hermes_read_actor.py")
        self.market = SSHReader(root/"scripts/market_gateway_read_actor.py") if direct else SSHReader(root/"scripts/market_read_actor.py",container=True)
        self.status = {"mode":"DEDICATED_PUBLIC_REST" if direct else "OPERATING_STORE_READ_ONLY", "poll_seconds":30,
                       "state":"CONNECTING", "orders_enabled":False, "production_db_changed":False}

    def poll(self):
        captured = self.reports.request({"operation":"final_reports"})
        if captured.get("error"):
            raise ConnectionError("HERMES_FINAL_REPORT_READ_FAILED")
        reports = []
        for row in captured["messages"]:
            try:
                reports.append(parse_report(row,captured["retrieved_at"]))
            except (ValueError,KeyError,TypeError):
                continue
        if not reports:
            raise ValueError("NO_VALID_FINAL_REPORT")
        self.accounts.save_reports(reports)
        ids = self.accounts.account_ids()
        if not ids:
            ids = [self.accounts.create_account()]
            self.accounts.worker_started()
        symbols = []
        for aid in ids:
            state = self.accounts.get_account(aid)["state"]
            symbols += list(state["positions"]) + list(state["pending"])
        latest = max(reports,key=lambda r:r["published_at"])
        symbols += [c["symbol"] for c in latest["candidates"]]
        symbols = list(dict.fromkeys(symbols))[:6]
        market = self.market.request({"operation":"public_market","symbols":symbols,"mode":"DIRECT" if self.direct else "STORE"}) if symbols else {"quotes":[]}
        now = utc_now()
        feed = {"mode":market.get("feed_mode",self.status["mode"]),"polled_at":now,"poll_seconds":30,
                "auth_mode":market.get("auth_mode"),
                "source_update_seconds":market.get("source_update_seconds"),"error":market.get("error"),
                "execution_gate":market.get("execution_gate","VERIFIED_RAW_REGULAR_OPEN_REQUIRED")}
        if self.direct and not self.allow_open_fills:
            feed["execution_gate"] = "OPEN_CAPTURE_POLICY_APPROVAL_REQUIRED"
        for aid in ids:
            self.accounts.update(aid,reports,market.get("quotes",[]),feed,market.get("calendar"),market.get("opened") if self.allow_open_fills else None,now)
        self.status.update(state="CONNECTED" if not market.get("error") else "MARKET_DATA_UNAVAILABLE",last_success_at=now, error=None,
                           source_update_seconds=feed["source_update_seconds"],market_error=market.get("error"))

    def run(self):
        self.accounts.worker_started()
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.poll()
                except Exception as exc:
                    self.status.update(state="SOURCE_UNAVAILABLE",error=type(exc).__name__,last_attempt_at=utc_now())
                self.stop.wait(max(1,30-(time.monotonic()-started)))
        finally:
            self.reports.close()
            self.market.close()
