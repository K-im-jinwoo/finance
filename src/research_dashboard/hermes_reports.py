"""Extract explicit CIO ranks; preserve the complete final answer and its lineage."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from .contracts import instant


def parse_report(message: dict, retrieved_at: str) -> dict:
    content = message["content"]
    if not isinstance(content, str) or not content or len(content) > 256_000:
        raise ValueError("bounded final report text required")
    published = datetime.fromtimestamp(float(message["timestamp"]), timezone.utc).isoformat()
    if instant(published) > instant(retrieved_at):
        raise ValueError("future Hermes final answer")
    sha = hashlib.sha256(content.encode()).hexdigest()
    if message.get("content_sha256", sha) != sha:
        raise ValueError("report content fingerprint mismatch")
    plain = content.replace("**", "").replace("`", "")
    ids = list(dict.fromkeys(re.findall(r"R-\d{8}T\d{4}Z-[A-F0-9]{8}", plain)))
    # Only explicitly numbered stock headings. A first unnumbered paragraph is not rank 1.
    headings = list(re.finditer(r"(?m)^\s*(?:#{1,6}\s*)?(\d{1,2})[.)]\s*([^\n()]+?)\s*\((\d{6})\)\s*$", plain))
    candidates = []
    for index, match in enumerate(headings):
        block = plain[match.end():headings[index+1].start() if index+1 < len(headings) else len(plain)]
        block = re.split(r"(?m)^#{1,6}\s", block, maxsplit=1)[0]
        def field(*labels):
            for label in labels:
                values = re.findall(r"(?m)^\s*-?\s*" + re.escape(label) + r"\s*:\s*(.+)$", block)
                if len(values) == 1:
                    return values[0].strip()
                if len(values) > 1:
                    return None
            return None
        tier = field("검토 등급", "review_tier")
        decision = field("행동 판정", "decision")
        candidates.append({"rank":int(match[1]), "name":match[2].strip(), "symbol":match[3],
                           "review_tier":{"우선 검토 후보":"PRIORITY_REVIEW", "일반 검토":"STANDARD_REVIEW"}.get(tier, tier),
                           "decision":{"매수 보류":"BUY_HOLD", "후보":"CANDIDATE", "제외":"EXCLUDED"}.get(decision, decision),
                           "original_review_tier":tier, "original_decision":decision,
                           "reason":field("핵심 이유"), "conditions":field("추가 확인 조건"), "invalidation":field("무효화 조건")})
    ranks = [c["rank"] for c in candidates]
    symbols = [c["symbol"] for c in candidates]
    valid = (message.get("finish_reason") == "stop" and bool(re.search(r"CIO\s*최종\s*판정", plain))
             and len(ids) == 1 and ranks.count(1) == 1 and len(ranks) == len(set(ranks))
             and len(symbols) == len(set(symbols)))
    first = next((c for c in candidates if c["rank"] == 1), None)
    if not first or first["decision"] not in {"BUY_HOLD", "CANDIDATE", "EXCLUDED"} or first["review_tier"] not in {"PRIORITY_REVIEW", "STANDARD_REVIEW"}:
        valid = False
    return {"key":"H-" + sha[:24], "report_id":ids[0] if len(ids) == 1 else None,
            "source":"HERMES_STOCK_CIO_FINAL_MESSAGE", "author":"stock-cio", "message_id":message["message_id"],
            "session_ref":message.get("session_ref"), "published_at":published, "retrieved_at":retrieved_at,
            "content_sha256":sha, "content":content, "candidates":candidates,
            "selection_status":"EXPLICIT_RANK_1" if valid else "UNSUPPORTED_OR_AMBIGUOUS",
            "selected":first if valid else None,
            "selection_basis":"Hermes CIO explicit rank 1 only; BUY_HOLD remains experimental, not approval"}
