from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


class ProviderError(RuntimeError):
    code = "PROVIDER_ERROR"


class AuthenticationError(ProviderError):
    code = "AUTHENTICATION_ERROR"


class RateLimitError(ProviderError):
    code = "RATE_LIMITED"


class NetworkError(ProviderError):
    code = "NETWORK_ERROR"


class UpstreamSchemaError(ProviderError):
    code = "UPSTREAM_SCHEMA_CHANGED"


@dataclass(frozen=True, slots=True)
class JsonResponse:
    status: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BinaryResponse:
    status: int
    payload: bytes


OpenFunction = Callable[..., Any]


def post_form_json(
    url: str,
    *,
    form: dict[str, str],
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 15,
    opener: OpenFunction = urllib.request.urlopen,
) -> JsonResponse:
    encoded = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "stock-assistant/0.1",
            **(headers or {}),
        },
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthenticationError(f"provider returned HTTP {exc.code}") from exc
        if exc.code == 429:
            raise RateLimitError("provider returned HTTP 429") from exc
        raise NetworkError(f"provider returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise NetworkError(f"provider request failed: {type(exc).__name__}") from exc
    if status in {401, 403}:
        raise AuthenticationError(f"provider returned HTTP {status}")
    if status == 429:
        raise RateLimitError("provider returned HTTP 429")
    if status < 200 or status >= 300:
        raise NetworkError(f"provider returned HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamSchemaError("provider response is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise UpstreamSchemaError("provider JSON root must be an object")
    return JsonResponse(status, payload)


def get_json(
    url: str,
    *,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 15,
    opener: OpenFunction = urllib.request.urlopen,
) -> JsonResponse:
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "stock-assistant/0.1", **(headers or {})},
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthenticationError(f"provider returned HTTP {exc.code}") from exc
        if exc.code == 429:
            raise RateLimitError("provider returned HTTP 429") from exc
        raise NetworkError(f"provider returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise NetworkError(f"provider request failed: {type(exc).__name__}") from exc
    if status in {401, 403}:
        raise AuthenticationError(f"provider returned HTTP {status}")
    if status == 429:
        raise RateLimitError("provider returned HTTP 429")
    if status < 200 or status >= 300:
        raise NetworkError(f"provider returned HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamSchemaError("provider response is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise UpstreamSchemaError("provider JSON root must be an object")
    return JsonResponse(status, payload)


def get_bytes(
    url: str,
    *,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
    opener: OpenFunction = urllib.request.urlopen,
) -> BinaryResponse:
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "stock-assistant/0.1", **(headers or {})},
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthenticationError(f"provider returned HTTP {exc.code}") from exc
        if exc.code == 429:
            raise RateLimitError("provider returned HTTP 429") from exc
        raise NetworkError(f"provider returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise NetworkError(f"provider request failed: {type(exc).__name__}") from exc
    if status in {401, 403}:
        raise AuthenticationError(f"provider returned HTTP {status}")
    if status == 429:
        raise RateLimitError("provider returned HTTP 429")
    if status < 200 or status >= 300:
        raise NetworkError(f"provider returned HTTP {status}")
    return BinaryResponse(status, raw)
