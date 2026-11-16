"""Standard-library frame/socket harness; no installed websocket library or I/O."""
from collections import deque
import json


class FakeServer:
    def __init__(self, rejected=()):
        self.rejected = set(rejected)
        self.sockets = []
        self.trace = []

    def connect(self):
        if any(not s.closed for s in self.sockets):
            raise AssertionError("old socket not closed before reconnect")
        self.trace.append("connect")
        s = FakeSocket(self)
        self.sockets.append(s)
        return s


class FakeSocket:
    def __init__(self, server):
        self.server, self.closed = server, False
        self.frames, self.sent = deque(), []
        self.topics = set()

    def send(self, raw):
        if self.closed:
            raise ConnectionError()
        self.sent.append(raw)
        if raw == "PING":
            self.frames.append(json.dumps({"type": "pong"}))
            return
        declaration = json.loads(raw)
        wanted = {row["type"] + ":" + code for row in declaration if "type" in row for code in row["codes"]}
        self.topics = wanted - self.server.rejected
        ack = {"type": "subscriptions", "subscribed": sorted(self.topics),
               "rejected": [{"target": t, "code": "stock-not-found", "message": "ignored"} for t in sorted(wanted & self.server.rejected)]}
        if declaration:
            ack["id"] = declaration[0]["id"]
        self.frames.append(json.dumps(ack))

    def push(self, frame):
        self.frames.append(json.dumps(frame))

    def recv(self, *, timeout):
        if self.closed:
            raise ConnectionError()
        if not self.frames:
            raise TimeoutError()
        return self.frames.popleft()

    def close(self):
        if not self.closed:
            self.server.trace.append("close")
        self.closed = True
