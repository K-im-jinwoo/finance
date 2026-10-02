from __future__ import annotations

import re


_KOREAN_SECURITY_SYMBOL = re.compile(r"^[0-9A-Z]{6}$")


def is_korean_security_symbol(value: object) -> bool:
    """Return whether value is a KRX/DART six-character short code."""
    return isinstance(value, str) and _KOREAN_SECURITY_SYMBOL.fullmatch(value) is not None


def require_korean_security_symbol(value: object, *, field: str = "symbol") -> str:
    if not is_korean_security_symbol(value):
        raise ValueError(
            f"{field} must be a six-character uppercase alphanumeric Korean security code"
        )
    return value
