from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .models import AssetType, CompanyKind, Decision
from .providers.dart import KST
from .reports import CandidateReport, build_candidate_report, report_to_dict
from .repository import StockRepository
from .screening import screen_security, select_top_candidates
from .validation import validate_analysis_time


@dataclass(frozen=True, slots=True)
class PipelineSummary:
    report: CandidateReport
    scanned: int
    excluded: int
    insufficient_history: int
    missing_financials: int
    missing_profit_periods: int
    missing_turnover_metrics: int
    missing_financial_company_metrics: int
    missing_etf_metrics: int
    missing_financing_histories: int
    missing_management_histories: int
    missing_catalyst_histories: int


class CandidatePipeline:
    """Create a deterministic top-five report from normalized, persisted inputs."""

    def __init__(self, repository: StockRepository) -> None:
        self.repository = repository

    def _screen_results(self, *, as_of: datetime, symbols: frozenset[str] | None = None):
        as_of = validate_analysis_time(as_of)
        securities = self.repository.list_securities()
        if not securities:
            raise ValueError("security universe is empty")
        if symbols is not None:
            securities = [security for security in securities if security.symbol in symbols]
            if not securities:
                raise ValueError("security is not in the stored universe")
        results = []
        insufficient_history = 0
        missing_financials = 0
        missing_profit_periods = 0
        missing_turnover_metrics = 0
        missing_financial_company_metrics = 0
        missing_etf_metrics = 0
        missing_financing_histories = 0
        missing_management_histories = 0
        missing_catalyst_histories = 0
        financing_since = as_of.astimezone(timezone.utc) - timedelta(days=365 * 5 + 2)
        management_since = as_of.astimezone(timezone.utc) - timedelta(days=365 * 10 + 3)
        catalyst_since = as_of.astimezone(timezone.utc) - timedelta(days=366)
        coverage_end = as_of.astimezone(KST).date() - timedelta(days=1)
        financing_coverage_start = max(coverage_end - timedelta(days=365 * 5 + 2), date(2015, 1, 1))
        management_coverage_start = max(coverage_end - timedelta(days=365 * 10 + 3), date(2015, 1, 1))
        catalyst_coverage_start = coverage_end - timedelta(days=366)
        for security in securities:
            bars = self.repository.bars_for(security.symbol, as_of=as_of, limit=120)
            financial = None
            financial_company = None
            etf_snapshot = None
            financing_events = ()
            management_risks = ()
            catalysts = ()
            financing_available = None
            management_available = None
            catalyst_available = None
            if security.asset_type is AssetType.COMMON:
                if security.company_kind is CompanyKind.FINANCIAL:
                    financial_company = self.repository.latest_financial_company(security.symbol, as_of=as_of)
                    if financial_company is None or any(value is None for value in (
                        financial_company.capital_adequacy_ratio,
                        financial_company.return_on_equity,
                        financial_company.non_performing_loan_ratio,
                        financial_company.delinquency_ratio,
                        financial_company.provision_coverage_ratio,
                        financial_company.shareholder_return_note,
                    )):
                        missing_financial_company_metrics += 1
                else:
                    financial = self.repository.latest_financial(security.symbol, as_of=as_of)
                    if financial is None:
                        missing_financials += 1
                        missing_profit_periods += 1
                        missing_turnover_metrics += 1
                    else:
                        if (
                            financial.annual_operating_income is None
                            or financial.ttm_operating_income is None
                            or financial.ttm_period_end is None
                            or financial.ttm_source_url is None
                            or (as_of.date() - financial.ttm_period_end).days > 185
                        ):
                            missing_profit_periods += 1
                        if len(financial.receivable_turnover) < 2 or len(financial.inventory_turnover) < 2:
                            missing_turnover_metrics += 1
                financing_events = tuple(self.repository.financing_events_for(
                    security.symbol,
                    as_of=as_of,
                    since=financing_since,
                ))
                financing_available = self.repository.has_coverage(
                    symbol=security.symbol,
                    dataset="DART_FINANCING",
                    required_start_date=financing_coverage_start.isoformat(),
                    required_end_date=coverage_end.isoformat(),
                    as_of=as_of,
                )
                if not financing_available:
                    missing_financing_histories += 1
                management_risks = tuple(self.repository.management_risks_for(
                    security.symbol, as_of=as_of, since=management_since,
                ))
                management_available = self.repository.has_coverage(
                    symbol=security.symbol,
                    dataset="DART_MANAGEMENT_RISK",
                    required_start_date=management_coverage_start.isoformat(),
                    required_end_date=coverage_end.isoformat(),
                    as_of=as_of,
                )
                if not management_available:
                    missing_management_histories += 1
                catalysts = tuple(self.repository.catalysts_for(
                    security.symbol, as_of=as_of, since=catalyst_since,
                ))
                catalyst_available = self.repository.has_coverage(
                    symbol=security.symbol,
                    dataset="DART_CATALYST",
                    required_start_date=catalyst_coverage_start.isoformat(),
                    required_end_date=coverage_end.isoformat(),
                    as_of=as_of,
                )
                if not catalyst_available:
                    missing_catalyst_histories += 1
            elif security.asset_type is AssetType.ETF:
                etf_snapshot = self.repository.latest_etf_snapshot(security.symbol, as_of=as_of)
                if etf_snapshot is None or any(value is None for value in (
                    etf_snapshot.net_assets,
                    etf_snapshot.premium_discount_pct,
                    etf_snapshot.tracking_error_pct,
                    etf_snapshot.total_expense_ratio_pct,
                    etf_snapshot.top10_weight_pct,
                )):
                    missing_etf_metrics += 1
            result = screen_security(
                security,
                bars,
                as_of=as_of,
                financial=financial,
                financial_company=financial_company,
                etf_snapshot=etf_snapshot,
                financing_events=financing_events,
                management_risks=management_risks,
                catalysts=catalysts,
                financing_data_available=financing_available,
                management_data_available=management_available,
                catalyst_data_available=catalyst_available,
            )
            if "DATA_UNAVAILABLE" in result.warnings and len(bars) < 61:
                insufficient_history += 1
            results.append(result)
        return (
            securities, results, insufficient_history, missing_financials,
            missing_profit_periods,
            missing_turnover_metrics,
            missing_financial_company_metrics, missing_etf_metrics,
            missing_financing_histories, missing_management_histories,
            missing_catalyst_histories,
        )

    def ranked_symbols(self, *, as_of: datetime, limit: int = 30) -> list[str]:
        _, results, _, _, _, _, _, _, _, _, _ = self._screen_results(as_of=as_of)
        return [item.symbol for item in select_top_candidates(results, limit=limit)]

    def screen_symbol(self, *, symbol: str, as_of: datetime):
        normalized_symbol = symbol.strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        _, results, _, _, _, _, _, _, _, _, _ = self._screen_results(
            as_of=as_of,
            symbols=frozenset({normalized_symbol}),
        )
        return results[0]

    def run(self, *, as_of: datetime, limit: int = 5) -> PipelineSummary:
        (
            securities, results, insufficient_history, missing_financials,
            missing_profit_periods,
            missing_turnover_metrics,
            missing_financial_company_metrics, missing_etf_metrics,
            missing_financing_histories, missing_management_histories,
            missing_catalyst_histories,
        ) = self._screen_results(as_of=as_of)
        selected = select_top_candidates(results, limit=limit)
        unavailable = [
            "실시간 장중 데이터",
            "공식 근거와 연결되지 않은 비공식 재료",
            "계약·실적 공시의 상세 조건과 현재 이행 상태",
        ]
        if missing_financials:
            unavailable.append(f"재무 미적재 종목 {missing_financials}개")
        if missing_profit_periods:
            unavailable.append(f"결산연도·TTM 영업이익 미적재·오래됨 종목 {missing_profit_periods}개")
        if missing_turnover_metrics:
            unavailable.append(f"매출채권·재고자산 회전율 추세 미적재 종목 {missing_turnover_metrics}개")
        if missing_financial_company_metrics:
            unavailable.append(f"금융회사 전용 지표 미적재·불완전 종목 {missing_financial_company_metrics}개")
        if missing_etf_metrics:
            unavailable.append(f"ETF 전용 지표 미적재·불완전 종목 {missing_etf_metrics}개")
        if insufficient_history:
            unavailable.append(f"61거래일 미만 종목 {insufficient_history}개")
        if missing_financing_histories:
            unavailable.append(f"5년 CB·BW·유상증자 이력 미적재 종목 {missing_financing_histories}개")
        if missing_management_histories:
            unavailable.append(f"10년 DART 경영진 위험 이력 미적재 종목 {missing_management_histories}개")
        if missing_catalyst_histories:
            unavailable.append(f"최근 1년 DART 계약·실적 공시 이력 미적재 종목 {missing_catalyst_histories}개")
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
            missing_profit_periods=missing_profit_periods,
            missing_turnover_metrics=missing_turnover_metrics,
            missing_financial_company_metrics=missing_financial_company_metrics,
            missing_etf_metrics=missing_etf_metrics,
            missing_financing_histories=missing_financing_histories,
            missing_management_histories=missing_management_histories,
            missing_catalyst_histories=missing_catalyst_histories,
        )
