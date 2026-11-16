"""Transport-independent JSON subscription protocol. No RFC6455 reimplementation."""
from __future__ import annotations

from collections import deque
import json

from .contracts import monotonic_value, symbol


def topics_checked(topics):
    values = set(topics)
    if len(values) > 100:
        raise ValueError("at most 100 channel-symbol subscriptions")
    for value in values:
        parts = value.split(":")
        if len(parts) != 3 or parts[0] not in {"trade", "orderbook"} or parts[1] != "kr":
            raise ValueError("public KR topics only")
        symbol(parts[2])
    return values


class WebSocketAdapter:
    """A caller drives connect/poll/tick using a monotonic clock and socket factory.

    Reconnect closes the prior socket first. auth-revoked never refreshes OAuth;
    the existing authentication owner must explicitly confirm restored auth.
    """
    def __init__(self, factory, *, rng, on_message=None, pong_timeout=30):
        if type(pong_timeout) is not int or not 1 <= pong_timeout <= 60:
            raise ValueError("invalid pong timeout")
        self.factory, self.rng, self.on_message = factory, rng, on_message
        self.pong_timeout = pong_timeout
        self.socket = None
        self.desired, self.active, self.rejected = set(), set(), set()
        self.pending = None
        self.declaration_seq = 0
        self.status = "DISCONNECTED"
        self.last_now = 0
        self.last_ping = 0
        self.waiting_pong = None
        self.retry_at = None
        self.failures = 0
        self.messages = deque(maxlen=1000)
        self.local_overflow = False
        self.declaration_times = deque()

    @property
    def entry_blocked(self):
        return self.status != "LIVE" or bool(self.rejected) or self.local_overflow

    def _time(self, now):
        monotonic_value(now)
        if now < self.last_now:
            raise ValueError("monotonic clock regressed")
        self.last_now = now

    def close(self):
        old, self.socket = self.socket, None
        self.active.clear()
        self.pending = None
        self.waiting_pong = None
        self.status = "DISCONNECTED"
        if old is not None:
            try:
                old.close()
            except Exception:
                self.socket = old
                self.status = "CLOSE_FAILED"
                raise

    def _schedule(self, now):
        self.close()
        self.failures += 1
        base = min(60, 2 ** min(self.failures - 1, 6))
        self.retry_at = now + base + self.rng.uniform(0, base * .25)
        self.status = "BACKOFF"

    def connect(self, *, now):
        self._time(now)
        if self.status == "AUTH_REVOKED":
            raise ValueError("shared authentication owner must confirm recovery")
        # Even explicit reconnect cannot overlap the previous connection.
        self.close()
        try:
            self.socket = self.factory()
            self.last_ping = now
            self.retry_at = None
            self._declare()
        except Exception:
            self._schedule(now)

    def replace(self, topics, *, now):
        self._time(now)
        values = topics_checked(topics)
        if self.pending is not None:
            raise ValueError("wait for previous declaration acknowledgement")
        if self.socket is not None:
            self._declaration_budget()
        self.desired, self.rejected = values, set()
        if self.socket is not None:
            self._declare()

    def _declare(self):
        """Handle send failure identically for initial connect and replacement."""
        self._declaration_budget()
        self.declaration_times.append(self.last_now)
        self.declaration_seq += 1
        request_id = "declare-" + str(self.declaration_seq)
        by_kind = {}
        for topic in sorted(self.desired):
            channel, country, code = topic.split(":")
            by_kind.setdefault(channel + ":" + country, []).append(code)
        # Empty array really means unsubscribe all; an id-only array isn't needed.
        declaration = ([{"id": request_id}] + [{"type": key, "codes": value} for key, value in sorted(by_kind.items())]) if by_kind else []
        self.pending = (request_id if declaration else None, set(self.desired))
        try:
            self.socket.send(json.dumps(declaration))
        except (OSError, ConnectionError):
            self._schedule(self.last_now)
            return
        self.status = "AWAITING_ACK"

    def _declaration_budget(self):
        while self.declaration_times and self.declaration_times[0] <= self.last_now - 1:
            self.declaration_times.popleft()
        if len(self.declaration_times) >= 5:
            raise ValueError("five declarations per second limit")

    def on_frame(self, raw, *, now):
        self._time(now)
        if not isinstance(raw, (str, bytes)) or len(raw) > 1_000_000:
            raise ValueError("bounded text frame required")
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            raise ValueError("object frame required")
        kind = frame.get("type")
        if kind == "subscriptions":
            if self.pending is None:
                raise ValueError("unexpected subscription acknowledgement")
            request_id, wanted = self.pending
            if request_id is not None and frame.get("id") != request_id:
                raise ValueError("acknowledgement identity mismatch")
            accepted = topics_checked(frame.get("subscribed", []))
            rejected_rows = frame.get("rejected", [])
            if not isinstance(rejected_rows, list):
                raise ValueError("invalid rejected list")
            rejected = topics_checked(r["target"] for r in rejected_rows)
            if accepted & rejected or accepted | rejected != wanted:
                raise ValueError("acknowledgement topic mismatch")
            self.active = accepted
            self.rejected.update(rejected)
            self.desired = accepted.copy()  # Do not automatically retry rejected topics.
            self.pending = None
            self.failures = 0
            self.status = "PARTIAL" if self.rejected else "LIVE"
        elif kind == "message":
            # Existing subscriptions remain authoritative until the new ack.
            if frame.get("topic") not in self.active:
                raise ValueError("data outside acknowledged public scope")
            data = frame.get("data")
            if not isinstance(data, dict) or data.get("currency") != "KRW":
                raise ValueError("KRW data required")
            if len(self.messages) == self.messages.maxlen:
                self.local_overflow = True
            self.messages.append(frame)
            if self.on_message is not None:
                self.on_message(frame)
        elif kind == "pong":
            self.waiting_pong = None
        elif kind == "error":
            error = frame.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code == "auth-revoked":
                self.close()
                self.retry_at = None
                self.status = "AUTH_REVOKED"
            elif code == "server-shutdown":
                self._schedule(now)
            else:
                self.close()
                self.status = "DATA_DEGRADED"
        else:
            raise ValueError("unsupported protocol frame")

    def poll(self, *, now):
        self._time(now)
        if self.socket is None:
            return
        try:
            raw = self.socket.recv(timeout=0)
        except TimeoutError:
            return
        except (OSError, ConnectionError):
            self._schedule(now)
            return
        try:
            self.on_frame(raw, now=now)
        except (ValueError, TypeError, KeyError):
            self.close()
            self.status = "DATA_DEGRADED"

    def tick(self, *, now):
        self._time(now)
        if self.status == "BACKOFF" and self.retry_at is not None and now >= self.retry_at:
            self.connect(now=now)
        elif self.socket is not None:
            if self.waiting_pong is not None and now - self.waiting_pong >= self.pong_timeout:
                self._schedule(now)
            elif now - self.last_ping >= 60:
                try:
                    self.socket.send("PING")
                except (OSError, ConnectionError):
                    self._schedule(now)
                    return
                self.last_ping = now
                self.waiting_pong = now

    def auth_restored(self, *, verified=False, now):
        if self.status != "AUTH_REVOKED" or not verified:
            raise ValueError("explicit shared-auth recovery required")
        self.status = "DISCONNECTED"
        self.connect(now=now)

    def snapshot(self):
        return {"status": self.status, "active": sorted(self.active), "rejected": sorted(self.rejected),
                "entry_blocked": self.entry_blocked, "complete_tape": False,
                "server_loss_detection": "UNAVAILABLE_NO_SEQUENCE", "local_overflow": self.local_overflow}


def runtime_socket_factory(token_supplier, *, slot_confirmed=False):
    """Optional live wiring only; creating the factory performs no connection."""
    def connect():
        if slot_confirmed is not True:
            raise ValueError("account websocket slot confirmation required")
        # Deliberately lazy: offline protocol tests don't import websockets.
        import logging
        from websockets.sync.client import connect as ws_connect
        logger = logging.Logger("daytrade-private", level=logging.CRITICAL + 1)
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        return ws_connect("wss://openapi-ws.tossinvest.com/ws/v1",
                          additional_headers={"Authorization": "Bearer " + token_supplier()},
                          open_timeout=10, close_timeout=5, max_size=1_000_000, max_queue=64,
                          compression=None, logger=logger)
    return connect
