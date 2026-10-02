from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .identifiers import require_korean_security_symbol
from .models import ScreeningResult, to_json_value


RULESET_VERSION = "2026-09-28.1"
REPORT_CONTRACT_VERSION = "1.2"


@dataclass(frozen=True, slots=True)
class CandidateReport:
    contract_version: str
    report_id: str
    as_of: datetime
    candidates: tuple[ScreeningResult, ...]
    fact_summary: tuple[str, ...]
    inference_summary: tuple[str, ...]
    assumptions: tuple[str, ...]
    unavailable: tuple[str, ...]
    ruleset_version: str = RULESET_VERSION
    news_discovery: dict | None = None


@dataclass(frozen=True, slots=True)
class JournalDraft:
    relative_path: str
    title: str
    markdown: str
    payload: dict


def make_report_id(
    as_of: datetime,
    results: list[ScreeningResult],
    *,
    facts: tuple[str, ...] = (),
    inferences: tuple[str, ...] = (),
    assumptions: tuple[str, ...] = (),
    unavailable: tuple[str, ...] = (),
    ruleset_version: str = RULESET_VERSION,
    news_discovery: dict | None = None,
) -> str:
    timestamp = as_of.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
    contents = {
        "results": [to_json_value(item) for item in results],
        "facts": facts,
        "inferences": inferences,
        "assumptions": assumptions,
        "unavailable": unavailable,
        "ruleset_version": ruleset_version,
    }
    if news_discovery is not None:
        contents["news_discovery"] = news_discovery
    identity = json.dumps(
        contents,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8].upper()
    return f"R-{timestamp}-{digest}"


def build_candidate_report(
    as_of: datetime,
    results: list[ScreeningResult],
    *,
    facts: tuple[str, ...] = (),
    inferences: tuple[str, ...] = (),
    assumptions: tuple[str, ...] = (),
    unavailable: tuple[str, ...] = (),
    ruleset_version: str = RULESET_VERSION,
    news_discovery: dict | None = None,
) -> CandidateReport:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return CandidateReport(
        "1.3" if news_discovery is not None else REPORT_CONTRACT_VERSION,
        make_report_id(
            as_of,
            results,
            facts=facts,
            inferences=inferences,
            assumptions=assumptions,
            unavailable=unavailable,
            ruleset_version=ruleset_version,
            news_discovery=news_discovery,
        ),
        as_of.astimezone(timezone.utc),
        tuple(results),
        facts, inferences, assumptions, unavailable, ruleset_version, news_discovery,
    )


_EVENT_SLUG = re.compile(r"^[a-z0-9-]+$")


def build_journal_draft(
    *,
    event_date: date,
    symbol: str,
    company_name: str,
    event_slug: str,
    report_id: str,
    confirmed_facts: tuple[str, ...],
    decisions: tuple[str, ...],
    private_position_lines: tuple[str, ...] = (),
) -> JournalDraft:
    require_korean_security_symbol(symbol)
    if not _EVENT_SLUG.fullmatch(event_slug):
        raise ValueError("event_slug must contain lowercase letters, numbers, and hyphens only")
    safe_name = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", company_name).strip("-")
    if not safe_name:
        raise ValueError("company_name is invalid")
    relative_path = f"wiki/20_Areas/Investments/{event_date.isoformat()}-{symbol}-{event_slug}.md"
    title = f"{event_date.isoformat()} {safe_name} 투자일지"
    facts_text = "\n".join(f"- {item}" for item in confirmed_facts) or "- 확인된 사실 없음"
    decision_text = "\n".join(f"- {item}" for item in decisions) or "- 결정 없음"
    position_text = "\n".join(f"- {item}" for item in private_position_lines) or "- 입력 없음"
    markdown = (
        "---\n"
        f'title: "{title}"\n'
        "type: area\n"
        "status: active\n"
        "domains:\n  - finance\n"
        "topics: []\n"
        f"created: {event_date.isoformat()}\n"
        f"updated: {event_date.isoformat()}\n"
        f"symbol: \"{symbol}\"\n"
        f"report_id: \"{report_id}\"\n"
        "---\n\n"
        f"# {title}\n\n"
        "## 확인된 사실\n\n"
        f"{facts_text}\n\n"
        "## 결정\n\n"
        f"{decision_text}\n\n"
        "## 보유정보\n\n"
        f"{position_text}\n"
    )
    payload = {
        "contract_version": "1.0",
        "operation": "create_or_match",
        "relative_path": relative_path,
        "content_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "report_id": report_id,
        "symbol": symbol,
    }
    return JournalDraft(relative_path, title, markdown, payload)


def report_to_dict(report: CandidateReport) -> dict:
    result = to_json_value(report)
    if report.news_discovery is None:
        result.pop("news_discovery")
    return result
