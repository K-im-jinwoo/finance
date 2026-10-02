from __future__ import annotations

from typing import Any


def _bullets(values: list[str] | tuple[str, ...], *, empty: str = "없음") -> str:
    return "\n".join(f"- {value}" for value in values) if values else f"- {empty}"


_VISIBLE_METRICS = (
    ("close", "종가"),
    ("volume_ratio20", "20일 대비 거래량"),
    ("drawdown60", "60일 고점 대비"),
    ("annual_operating_income", "최근 결산연도 영업이익"),
    ("ttm_operating_income", "최근 12개월 영업이익"),
    ("ttm_period_end", "TTM 기준일"),
    ("dilution_count_5y", "5년 희석성 조달 횟수"),
    ("max_dilution_ratio_pct_5y", "5년 최대 잠재 희석률(%)"),
    ("refixing_event_count_5y", "5년 리픽싱 포함 건수"),
    ("financing_purposes_5y", "확인된 조달 목적"),
    ("etf_nav_per_share", "ETF 주당 NAV"),
    ("etf_net_assets", "ETF 순자산총액"),
    ("etf_premium_discount_pct", "ETF 괴리율(%)"),
    ("etf_tracking_error_pct", "ETF 추적오차(%)"),
    ("etf_total_expense_ratio_pct", "ETF 총보수율(%)"),
    ("etf_top10_weight_pct", "ETF 상위 10종목 비중(%)"),
    ("capital_adequacy_ratio", "금융회사 자본적정성 비율"),
    ("return_on_equity", "금융회사 ROE"),
    ("non_performing_loan_ratio", "금융회사 부실채권 비율"),
    ("delinquency_ratio", "금융회사 연체율"),
    ("provision_coverage_ratio", "금융회사 충당금 커버리지"),
    ("shareholder_return_note", "금융회사 주주환원"),
)

_DECISION_LABELS = {
    "CANDIDATE": "검토 후보",
    "BUY_HOLD": "매수 보류",
    "EXCLUDED": "제외",
}

_REVIEW_TIER_LABELS = {
    "PRIORITY_REVIEW": "우선 검토 후보",
    "STANDARD_REVIEW": "일반 검토",
    "EXCLUDED": "제외",
}


def _visible_metrics(value: Any) -> str:
    if not isinstance(value, dict):
        return "- 확인 불가"
    lines = [
        f"- {label}: {value[key]}"
        for key, label in _VISIBLE_METRICS
        if value.get(key) is not None
    ]
    return "\n".join(lines) if lines else "- 확인 가능한 핵심지표 없음"


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
        (
            f"국내주식 후보 보고서\n보고서 ID: {report_id}"
            f"\n규칙 버전: {report.get('ruleset_version', 'legacy-unversioned')}"
            f"\n기준시각: {report.get('as_of', '확인 불가')}"
        ),
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
            f"시장/유형: {item.get('market', '확인 불가')} / {item.get('asset_type', '확인 불가')}",
            f"검토 등급: {_REVIEW_TIER_LABELS.get(str(item.get('review_tier')), '일반 검토')}",
            f"행동 판정: {_DECISION_LABELS.get(str(item.get('decision')), '확인 불가')} / 점수: {item.get('score', '확인 불가')}",
            f"전략/예상 기간: {item.get('strategy', '확인 불가')} / {item.get('expected_holding_period', '확인 불가')}",
            "핵심지표:",
            _visible_metrics(item.get("metrics")),
            "근거:",
            _bullets(item.get("reasons", [])),
            "경고:",
            _bullets(item.get("warnings", [])),
            "추가 확인:",
            _bullets(item.get("checks", [])),
            "무효화 조건:",
            _bullets(item.get("invalidation_conditions", []), empty="확인 불가"),
            "재료 상태:",
            _bullets(item.get("catalyst_states", []), empty="확인 불가"),
            "근거 출처:",
            _bullets(item.get("evidence_urls", []), empty="확인 불가"),
        ]
        sections.append("\n".join(lines))

    discovery = report.get("news_discovery")
    if discovery is not None:
        if not isinstance(discovery, dict):
            raise ValueError("news_discovery must be an object")
        if discovery.get("status") != "OK":
            sections.append("뉴스 발견 — 추가 검토\n뉴스 수집: 확인 불가\n기존 가격·재무 후보는 별도로 유지합니다.")
        else:
            sections.append("뉴스 발견 — 추가 검토\n기사에 보도된 재료이며, 공시·회사 발표와 대조가 필요합니다."
                            f"\n수집시각: {discovery.get('observed_at', '확인 불가')}")
            news_candidates = discovery.get("candidates", [])
            if not news_candidates:
                sections.append("검색한 최근 기사에서 새 검토 대상이 확인되지 않았습니다.")
            for index, candidate in enumerate(news_candidates, start=1):
                screening = candidate.get("screening", {})
                label = "신규 재료" if candidate.get("change") == "NEW" else "기존 재료 유지"
                status = "기존 규칙상 제외" if candidate.get("status") == "EXCLUDED" else "추가 검토 / 매수 보류"
                sections.append(
                    f"뉴스 후보 {index}. {candidate['name']} ({candidate['symbol']})\n변화: {label}"
                    f"\n판정: {status}\n기존 선별 점수: {screening.get('score', '확인 불가')}"
                    f"\n가격 기준일: {candidate.get('daily_price_date') or '확인 불가'}"
                    "\n추가 확인:\n" + _bullets(candidate.get("checks", []))
                )
                if candidate.get("status") == "EXCLUDED":
                    sections.append(f"{candidate['name']} 기존 규칙 제외 근거:\n" + _bullets(screening.get("warnings", [])))
                for evidence in candidate.get("evidence", []):
                    sections.append(
                        f"{candidate['name']} 기사: {evidence['title']}"
                        f"\n재료 분류: {evidence['category']}"
                        f"\n공급자 기사시각: {evidence['published_at']}"
                        f"\n최초 발견시각: {evidence.get('first_seen_at', evidence['observed_at'])}"
                        f"\n원문: {evidence['url']}"
                    )

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
