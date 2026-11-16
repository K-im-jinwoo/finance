"""Bounded public-market observations; reuse a valid shared token without renewal.

Run explicitly, outside tests. No database, account endpoint, credential renewal,
remote installation, scheduler, or outbound message. stdout contains aggregates.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = "https://openapi.tossinvest.com"
WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
READ_PATHS = frozenset({"/api/v1/rankings", "/api/v1/prices", "/api/v1/orderbook",
                        "/api/v1/trades", "/api/v1/candles"})
RATE_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After")


class ProbeStopped(Exception):
    """Only fixed, non-sensitive error codes may cross the CLI boundary."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeStopped("REDIRECT_REFUSED")


def utc_now():
    return datetime.now(timezone.utc)


def symbols_checked(symbols):
    if (not isinstance(symbols, list) or not 1 <= len(symbols) <= 5
            or len(set(symbols)) != len(symbols)
            or any(not isinstance(s, str) or re.fullmatch(r"[0-9A-Z]{6}", s) is None for s in symbols)):
        raise ProbeStopped("INVALID_PROBE_SYMBOLS")
    return symbols


def read_existing_token(cache, *, minimum_ttl, now=time.time):
    # Do not create an empty cache or lock file, issue OAuth, or invalidate a token.
    if (not cache.directory.is_dir() or not cache.path.is_file()
            or not (cache.directory / "toss-token.lock").is_file()):
        raise ProbeStopped("EXISTING_SHARED_CACHE_REQUIRED")
    with cache.locked():
        value = cache.read()
        if not value or value["valid_until"] - now() < minimum_ttl:
            raise ProbeStopped("SHARED_TOKEN_TTL_INSUFFICIENT_NO_RENEWAL")
        return value["token"]


def shape(value):
    """Field inventory only, never include arbitrary response bodies or errors."""
    if isinstance(value, dict):
        return {key: sorted(item) if isinstance(item, dict) else
                {"count": len(item), "first_fields": sorted(item[0]) if item and isinstance(item[0], dict) else []}
                if isinstance(item, list) else type(item).__name__
                for key, item in value.items()}
    if isinstance(value, list):
        return {"count": len(value), "first_fields": sorted(value[0]) if value and isinstance(value[0], dict) else []}
    return {"type": type(value).__name__}


class PublicRestProbe:
    def __init__(self, token, *, opener=None, pause=time.sleep, clock=time.monotonic):
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self._pause, self._clock = pause, clock
        self.observations = []
        self.stopped = False

    def get(self, path, query):
        if self.stopped:
            raise ProbeStopped("PROBE_ALREADY_STOPPED")
        if path not in READ_PATHS or len(self.observations) >= 10:
            raise ProbeStopped("READ_SCOPE_OR_REQUEST_BUDGET_EXCEEDED")
        self._pause(0.5)
        started, started_at = self._clock(), utc_now().isoformat()
        request = urllib.request.Request(BASE + path + "?" + urllib.parse.urlencode(query),
            headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"}, method="GET")
        row = {"path": path, "started_at": started_at}
        try:
            try:
                response = self._opener.open(request, timeout=10)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                row["http_status"] = response.status
                row["rate_headers"] = {key: str(response.headers[key]) for key in RATE_HEADERS
                    if response.headers.get(key) is not None
                    and re.fullmatch(r"[0-9.]{1,32}", str(response.headers[key]))}
                if response.status != 200:
                    from stock_assistant.daytrade.http import safe_error
                    row.update(safe_error(response.read(65537), response.headers, secrets=(self._token,)))
                    raise ProbeStopped("PUBLIC_HTTP_" + str(response.status))
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ProbeStopped("PUBLIC_RESPONSE_TOO_LARGE")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or "result" not in payload:
                    raise ProbeStopped("PUBLIC_RESPONSE_SCHEMA_UNEXPECTED")
                row["result_shape"] = shape(payload["result"])
                return payload["result"]
        except Exception:
            self.stopped = True
            raise
        finally:
            row.update(received_at=utc_now().isoformat(), elapsed_ms=round((self._clock() - started) * 1000, 3))
            self.observations.append(row)


def sample_rest(probe, symbols, *, skip_candles=False):
    symbols_checked(symbols)
    if type(skip_candles) is not bool:
        raise ProbeStopped("INVALID_SKIP_CANDLES_OPTION")
    for ranking in ("MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME"):
        result = probe.get("/api/v1/rankings", {"marketCountry": "KR", "duration": "realtime",
            "type": ranking, "count": 100, "excludeInvestmentCaution": "true"})
        probe.observations[-1]["ranking_type"] = ranking
        probe.observations[-1]["ranked_at"] = result.get("rankedAt") if isinstance(result, dict) else None
    probe.get("/api/v1/prices", {"symbols": ",".join(symbols)})
    # Two symbols only: the point is schema and latency, not market-wide load.
    for symbol in symbols[:2]:
        requests = [("/api/v1/orderbook", {"symbol": symbol}), ("/api/v1/trades", {"symbol": symbol})]
        if not skip_candles:
            requests.append(("/api/v1/candles", {"symbol": symbol, "interval": "1m", "count": 5, "adjusted": "false"}))
        for path, query in requests:
            probe.get(path, query)
    return probe.observations


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "p50_ms": None, "p95_ms": None}
    return {"samples": len(ordered), "p50_ms": round(ordered[math.ceil(len(ordered) * .5) - 1], 3),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3)}


def _probe_frame(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > 1_000_000:
        raise ProbeStopped("WEBSOCKET_FRAME_SCHEMA_UNEXPECTED")
    frame = json.loads(raw)
    if not isinstance(frame, dict):
        raise ProbeStopped("WEBSOCKET_FRAME_SCHEMA_UNEXPECTED")
    return frame


def _probe_ack(frame, expected, request_id):
    accepted, rejected = frame.get("subscribed"), frame.get("rejected")
    if (frame.get("id") != request_id or not isinstance(accepted, list)
            or any(not isinstance(t, str) for t in accepted) or len(accepted) != len(expected)
            or set(accepted) != expected or not isinstance(rejected, list) or rejected):
        raise ProbeStopped("WEBSOCKET_SUBSCRIPTION_NOT_FULLY_ACCEPTED")


def _close_probe_socket(ws):
    # close() must complete even if unsubscribe fails. A raised close prevents
    # the caller from proceeding to a replacement connection.
    try:
        ws.send("[]")
    finally:
        ws.close()


def sample_websocket(token, symbols, *, slot_available=False, seconds=15, connect=None,
                     verify_reconnect=False, clock=None, wall_clock=None):
    symbols_checked(symbols)
    if not slot_available:
        raise ProbeStopped("ACCOUNT_WEBSOCKET_SLOT_CONFIRMATION_REQUIRED")
    if type(seconds) is not int or not 5 <= seconds <= 30:
        raise ProbeStopped("INVALID_WEBSOCKET_SAMPLE_DURATION")
    if type(verify_reconnect) is not bool:
        raise ProbeStopped("INVALID_RECONNECT_OPTION")
    library_version = "INJECTED_SOCKET"
    if connect is None:
        import websockets
        from websockets.sync.client import connect
        library_version = websockets.__version__
    clock, wall_clock = clock or time.monotonic, wall_clock or utc_now
    logger = logging.Logger("intraday-probe-private", level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    expected = {f"{channel}:kr:{symbol}" for channel in ("trade", "orderbook") for symbol in symbols}
    out = {"status": "SAMPLING", "requested_topics": len(expected), "library_version": library_version,
           "counts": {}, "acknowledged_topics": 0, "rejected_topics": 0,
           "pong_received": False, "pong_rtt_ms": None,
           "null_timestamp_counts": {"trade": 0, "orderbook": 0},
           "reconnect": {"requested": verify_reconnect, "attempted": False, "status": "NOT_REQUESTED"},
           "source_loss_detection": "UNAVAILABLE_NO_SEQUENCE", "complete_tape": False}
    counts, ages, fields = Counter(), {"trade": [], "orderbook": []}, {}

    def open_socket():
        started = clock()
        ws = connect(WS_URL, additional_headers={"Authorization": "Bearer " + token},
                     open_timeout=10, close_timeout=5, max_size=1_000_000, max_queue=64,
                     compression=None, logger=logger)
        return ws, round((clock() - started) * 1000, 3)

    def declare(ws, request_id):
        ws.send(json.dumps([{"id": request_id}, {"type": "trade:kr", "codes": symbols},
                            {"type": "orderbook:kr", "codes": symbols}]))

    # No automatic retry, concurrent connection, personal topic, or token renewal.
    ws, out["handshake_ms"] = open_socket()
    ping_at = None
    try:
        declare(ws, "probe-1")
        deadline = clock() + seconds
        while clock() < deadline:
            try:
                frame = _probe_frame(ws.recv(timeout=max(.01, min(2, deadline - clock()))))
            except TimeoutError:
                continue
            kind = frame.get("type")
            if kind == "subscriptions":
                if out["acknowledged_topics"]:
                    raise ProbeStopped("WEBSOCKET_ACK_UNEXPECTED")
                _probe_ack(frame, expected, "probe-1")
                out.update(acknowledged_topics=len(expected), rejected_topics=0)
                ping_at = clock()
                ws.send("PING")
            elif kind == "pong":
                if ping_at is not None and not out["pong_received"]:
                    elapsed = clock() - ping_at
                    if elapsed < 0:
                        raise ProbeStopped("MONOTONIC_CLOCK_REVERSED")
                    out.update(pong_received=True, pong_rtt_ms=round(elapsed * 1000, 3))
            elif kind == "error":
                raise ProbeStopped("WEBSOCKET_SERVER_ERROR")
            elif kind == "message":
                topic, data = frame.get("topic"), frame.get("data")
                if topic not in expected or not isinstance(data, dict) or not out["acknowledged_topics"]:
                    raise ProbeStopped("WEBSOCKET_DATA_SCOPE_UNEXPECTED")
                if data.get("currency") != "KRW":
                    raise ProbeStopped("WEBSOCKET_CURRENCY_UNEXPECTED")
                channel = topic.split(":", 1)[0]
                counts[topic] += 1
                fields[channel] = sorted(data)
                stamp = data.get("timestamp")
                if stamp is None:
                    out["null_timestamp_counts"][channel] += 1
                elif len(ages[channel]) < 10000:
                    event_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if event_at.tzinfo is None:
                        raise ProbeStopped("WEBSOCKET_TIMESTAMP_TIMEZONE_REQUIRED")
                    ages[channel].append((wall_clock() - event_at).total_seconds() * 1000)
            else:
                raise ProbeStopped("WEBSOCKET_FRAME_SCHEMA_UNEXPECTED")
    finally:
        _close_probe_socket(ws)
    out["first_socket_closed"] = True
    out.update(status="ACK_UNOBSERVED" if not out["acknowledged_topics"] else
               "PONG_UNOBSERVED" if not out["pong_received"] else "OBSERVED", counts=dict(counts),
               data_fields=fields, event_age_ms={key: distribution(value) for key, value in ages.items()},
               negative_age_samples=sum(v < 0 for group in ages.values() for v in group),
               note="event age includes clock offset; no sequence means no proof of zero loss")
    if verify_reconnect:
        if out["status"] != "OBSERVED":
            out["reconnect"]["status"] = "NOT_RUN_FIRST_CONNECTION_INCOMPLETE"
            return out
        # This line is unreachable until the first synchronous close succeeded.
        second, duration = open_socket()
        reconnect = {"requested": True, "attempted": True, "status": "ACK_UNOBSERVED",
                     "handshake_ms": duration, "acknowledged_topics": 0}
        try:
            declare(second, "probe-reconnect-1")
            deadline = clock() + min(5, seconds)
            while clock() < deadline:
                try:
                    frame = _probe_frame(second.recv(timeout=max(.01, min(1, deadline - clock()))))
                except TimeoutError:
                    continue
                if frame.get("type") != "subscriptions":
                    raise ProbeStopped("WEBSOCKET_RECONNECT_ACK_UNEXPECTED")
                _probe_ack(frame, expected, "probe-reconnect-1")
                reconnect.update(status="OBSERVED", acknowledged_topics=len(expected))
                break
        finally:
            _close_probe_socket(second)
        reconnect["socket_closed"] = True
        out["reconnect"] = reconnect
        if reconnect["status"] != "OBSERVED":
            out["status"] = "RECONNECT_ACK_UNOBSERVED"
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id-file", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--symbols", default="005930,000660")
    parser.add_argument("--websocket-slot-available", action="store_true")
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--skip-candles", action="store_true")
    parser.add_argument("--verify-reconnect", action="store_true")
    args = parser.parse_args(argv)
    result = {"observed_at": utc_now().isoformat(), "mode": "BOUNDED_READ_ONLY_SPIKE",
              "orders_enabled": False, "database_opened": False, "token_issued": False,
              "token_invalidated": False, "rest": [], "websocket": {"status": "NOT_RUN_SLOT_UNCONFIRMED"}}
    cache = None
    before = None
    probe = None
    try:
        if not 5 <= args.seconds <= 30:
            raise ProbeStopped("INVALID_WEBSOCKET_SAMPLE_DURATION")
        if args.verify_reconnect and not args.websocket_slot_available:
            raise ProbeStopped("ACCOUNT_WEBSOCKET_SLOT_CONFIRMATION_REQUIRED")
        symbols = symbols_checked(args.symbols.split(","))
        from shared_toss_auth import SharedTokenCache
        cache = SharedTokenCache(args.cache_dir, Path(args.client_id_file).read_text().strip())
        token = read_existing_token(cache, minimum_ttl=args.seconds + 180 + (15 if args.verify_reconnect else 0))
        before = hashlib.sha256(cache.path.read_bytes()).hexdigest()
        probe = PublicRestProbe(token)
        sample_rest(probe, symbols, skip_candles=args.skip_candles)
        if args.websocket_slot_available:
            result["websocket"] = sample_websocket(token, symbols, slot_available=True, seconds=args.seconds,
                                                    verify_reconnect=args.verify_reconnect)
            if result["websocket"]["status"] != "OBSERVED":
                raise ProbeStopped("WEBSOCKET_PROBE_INCOMPLETE")
        result["status"] = "OBSERVED"
    except ProbeStopped as exc:
        result.update(status="STOPPED", error_code=str(exc))
    except Exception as exc:
        # Exception text and tracebacks may contain authorization headers.
        result.update(status="STOPPED", error_class=type(exc).__name__)
    finally:
        if probe is not None:
            result["rest"] = probe.observations
        if cache is not None and before is not None:
            try:
                result["cache_bytes_unchanged"] = hashlib.sha256(cache.path.read_bytes()).hexdigest() == before
            except Exception:
                result["cache_bytes_unchanged"] = None
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "OBSERVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
