"""Remove opaque model citations from stock-cio Telegram prose, preserving code and links."""
from __future__ import annotations

import os
import re
from pathlib import Path


# These controls refer to a provider's private search session, not a public URL.
_COMPLETE = r"\ue200cite\ue202[^\ue200\ue201\r\n]{1,4096}\ue201"
_REF = r"turn[0-9]+[a-zA-Z]+[0-9]+"
_INCOMPLETE = r"\ue200cite\ue202" + _REF + r"(?:\ue202" + _REF + r")*(?=[ \t]*(?:\r?\n|$))"
_CITATION = re.compile(r"[ \t]*(?:" + _COMPLETE + "|" + _INCOMPLETE + ")")
_CODE = re.compile(
    r"(?P<fence>^[ \t]{0,3}(?P<run>`{3,}|~{3,})[^\n]*\n"
    r".*?(?:^[ \t]{0,3}(?P=run)(?:(?<=`)`*|(?<=~)~*)[ \t]*(?:\r?\n|$)|\Z))"
    r"|(?P<inline>(?P<ticks>`+)(?!`).*?(?P=ticks)(?!`))",
    re.MULTILINE | re.DOTALL,
)


def remove_opaque_citations(text: str) -> str:
    """Delete citation metadata only outside code; never invent or rewrite source URLs."""
    if "\ue200cite\ue202" not in text:
        return text
    output = []
    cursor = 0
    for match in _CODE.finditer(text):
        output.append(_CITATION.sub("", text[cursor:match.start()]))
        output.append(match.group())
        cursor = match.end()
    output.append(_CITATION.sub("", text[cursor:]))
    return "".join(output)


def sanitize_stock_cio_citations(platform, text: str) -> str:
    """Profile-scoped boundary; other profiles and raw/programmatic surfaces are unchanged."""
    if getattr(platform, "value", platform) != "telegram":
        return text
    if Path(os.environ.get("HERMES_HOME", "")).name != "stock-cio":
        return text
    return remove_opaque_citations(text)
