from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import AssetType, Decision
from .reports import CandidateReport, build_candidate_report, report_to_dict
from .repository import StockRepository
from .screening import screen_security, select_top_candidates


@dataclass(frozen=True, slots=True)
class PipelineSummary:
    report: CandidateReport
    scanned: int
    excluded: int
    insufficient_history: int
    missing_financials: int


class CandidatePipeline:
    """Create a deterministic top-five report from normalized, persisted inputs."""

    def __init__(self, repository: StockRepository) -> None:
        self.repository = repository

    def _screen_results(self, *, as_of: datetime):
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        securities = self.repository.list_securities()
        if not securities:
            raise ValueError("security universe is empty")
        results = []
        insufficient_history = 0
        missing_financials = 0
        for security in securities:
            bars = self.repository.bars_for(security.symbol, as_of=as_of, limit=120)
            financial = None
            if security.asset_type is AssetType.COMMON:
                financial = self.repository.latest_financial(security.symbol, as_of=as_of)
                if financial is None:
                    missing_financials += 1
            result = screen_security(
                security,
                bars,
                as_of=as_of,
                financial=financial,
            )
            if "DATA_UNAVAILABLE" in result.warnings and len(bars) < 61:
                insufficient_history += 1
            results.append(result)
        return securities, results, insufficient_history, missing_financials

    def ranked_symbols(self, *, as_of: datetime, limit: int = 30) -> list[str]:
        _, results, _, _ = self._screen_results(as_of=as_of)
        return [item.symbol for item in select_top_candidates(results, limit=limit)]

    def run(self, *, as_of: datetime, limit: int = 5) -> PipelineSummary:
        securities, results, insufficient_history, missing_financials = self._screen_results(as_of=as_of)
        selected = select_top_candidates(results, limit=limit)
        unavailable = [
            "실시간 장중 데이터",
            "공식 근거와 연결되지 않은 비공식 재료",
            "계약·실적 외 공시 재료의 자동 유효성 분류",
            "CB·BW·유상증자 및 경영진 위험의 장기 이력 자동분류",
            "매출채권·재고자산 회전율 원천 계정과 추세",
        ]
        if missing_financials:
            unavailable.append(f"재무 미적재 종목 {missing_financials}개")
        if insufficient_history:
            unavailable.append(f"61거래일 미만 종목 {insufficient_history}개")
        report = build_candidate_report(
            as_of,
            selected,
            facts=(f"정규화 저장소의 종목 {len(securities)}개를 동일 기준시각으로 검사",),
            inferences=(),
            assumptions=("입력 데이터의 공급자 관측시각과 공시시각이 정확하다는 전제",),
            unavailable=tuple(unavailable),
        )
        payload = report_to_dict(report)
        self.repository.save_screening_results(report.report_id, selected)
        self.repository.save_report(report.report_id, report.as_of.isoformat(), payload)
        return PipelineSummary(
            report=report,
            scanned=len(results),
            excluded=sum(item.decision is Decision.EXCLUDED for item in results),
            insufficient_history=insufficient_history,
            missing_financials=missing_financials,
        )
