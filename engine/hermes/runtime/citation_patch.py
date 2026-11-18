"""Version-checked insertion into Hermes' existing final-response sanitizer."""
from __future__ import annotations

import ast


MARKER = "FINANCE_STOCK_CIO_CITATIONS_V1"
ANCHOR = "    text = _sanitize_surrogates(str(text))\n"
INSERT = (
    "\n    # " + MARKER + ": retain public links, remove opaque search-session citations.\n"
    "    from gateway.stock_cio_citations import sanitize_stock_cio_citations\n"
    "    text = sanitize_stock_cio_citations(platform, text)\n"
)


def patch_gateway(source: str) -> str:
    """Fail closed on upstream drift; preserve every byte outside the one insertion."""
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_sanitize_gateway_final_response"]
    if len(functions) != 1:
        raise ValueError("SANITIZER_MISSING_OR_AMBIGUOUS")
    lines = source.splitlines(keepends=True)
    node = functions[0]
    start = sum(map(len, lines[:node.lineno - 1]))
    end = sum(map(len, lines[:node.end_lineno]))
    body = source[start:end]
    if MARKER in source:
        if body.count(ANCHOR + INSERT) != 1 or source.count(MARKER) != 1:
            raise ValueError("UNEXPECTED_EXISTING_PATCH")
        return source
    if body.count(ANCHOR) != 1:
        raise ValueError("SANITIZER_ANCHOR_CHANGED")
    result = source[:start] + body.replace(ANCHOR, ANCHOR + INSERT, 1) + source[end:]
    ast.parse(result)
    return result
