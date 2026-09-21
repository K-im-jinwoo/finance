from __future__ import annotations

from typing import Any


def _bullets(values: list[str] | tuple[str, ...], *, empty: str = "없음") -> str:
    return "\n".join(f"- {value}" for value in values) if values else f"- {empty}"


def render_candidate_report(report: dict[str, Any], *, max_chars: int = 3500) -> tuple[str, ...]:
    """Render one stored report for Desktop or Telegram without changing its meaning."""
    if max_chars < 500 or max_chars > 4000:
        raise ValueError("max_chars must be between 500 and 4000")
    report_id = str(report.get("report_id", "")).strip()
    if not report_id.startswith("R-"):
        raise ValueError("report_id is required")
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a list")

    sections = [
        f"국내주식 후보 보고서\n보고서 ID: {report_id}\n기준시각: {report.get('as_of', '확인 불가')}",
        "확인된 사실\n" + _bullets(report.get("fact_summary", []), empty="확인된 사실 없음"),
        "추론\n" + _bullets(report.get("inference_summary", [])),
        "가정\n" + _bullets(report.get("assumptions", [])),
        "확인 불가\n" + _bullets(report.get("unavailable", [])),
    ]
    for index, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            raise ValueError("candidate must be an object")
        lines = [
            f"후보 {index}. {item.get('name') or '종목명 확인 불가'} ({item.get('symbol', '확인 불가')})",
            f"판정: {item.get('decision', '확인 불가')} / 점수: {item.get('score', '확인 불가')}",
            "근거:",
            _bullets(item.get("reasons", [])),
            "경고:",
            _bullets(item.get("warnings", [])),
            "추가 확인:",
            _bullets(item.get("checks", [])),
        ]
        sections.append("\n".join(lines))

    prefix = f"[{report_id}]\n"
    chunks: list[str] = []
    current = prefix
    for section in sections:
        addition = ("\n\n" if current != prefix else "") + section
        if len(prefix) + len(section) > max_chars:
            raise ValueError("one report section exceeds the channel message limit")
        if len(current) + len(addition) > max_chars:
            chunks.append(current)
            current = prefix + section
        else:
            current += addition
    chunks.append(current)
    return tuple(chunks)
