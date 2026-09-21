from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .indicators import InsufficientHistory, price_features, trend_is_stable_or_rising
from .models import (
    AssetType,
    Catalyst,
    CatalystStatus,
    CompanyKind,
    Decision,
    FinancialSnapshot,
    FinancingEvent,
    ManagementRisk,
    OHLCV,
    ScreeningResult,
    Security,
)
from .validation import assert_point_in_time


ALLOWED_ASSET_TYPES = {AssetType.COMMON, AssetType.ETF}


def _current_catalyst_status(catalyst: Catalyst, as_of: datetime) -> CatalystStatus:
    assert_point_in_time(as_of, published_at=catalyst.announced_at, label="catalyst")
    for evidence in catalyst.evidence:
        assert_point_in_time(
            as_of,
            observed_at=evidence.observed_at,
            published_at=evidence.published_at,
            label="catalyst evidence",
        )
    if catalyst.valid_until is not None and catalyst.valid_until < as_of.astimezone(timezone.utc):
        return CatalystStatus.EXPIRED
    return catalyst.status


def screen_security(
    security: Security,
    bars: list[OHLCV],
    *,
    as_of: datetime,
    financial: FinancialSnapshot | None = None,
    financing_events: tuple[FinancingEvent, ...] = (),
    management_risks: tuple[ManagementRisk, ...] = (),
    catalysts: tuple[Catalyst, ...] = (),
    financing_data_available: bool | None = None,
    management_data_available: bool | None = None,
    catalyst_data_available: bool | None = None,
    minimum_average_value: Decimal = Decimal("1000000000"),
) -> ScreeningResult:
    reasons: list[str] = []
    warnings: list[str] = []
    checks: list[str] = []
    score = Decimal("0")

    if security.asset_type not in ALLOWED_ASSET_TYPES:
        return ScreeningResult(
            security.symbol, as_of, Decision.EXCLUDED, score,
            ("UNSUPPORTED_ASSET_TYPE",), (), (), {}, security.name,
        )
    reasons.append("ELIGIBLE_ETF" if security.asset_type is AssetType.ETF else "ELIGIBLE_COMMON")

    usable_bars = []
    for bar in bars:
        assert_point_in_time(as_of, observed_at=bar.observed_at, label="OHLCV")
        if bar.symbol != security.symbol:
            raise ValueError("price history symbol does not match security")
        if bar.trade_date <= as_of.date():
            usable_bars.append(bar)
    try:
        features = price_features(usable_bars)
    except InsufficientHistory:
        return ScreeningResult(
            security.symbol, as_of, Decision.BUY_HOLD, score,
            tuple(reasons), ("DATA_UNAVAILABLE",),
            ("최소 61거래일의 검증된 OHLCV를 확보할 것",), {}, security.name,
        )

    if features.average_value20 < minimum_average_value:
        warnings.append("LIQUIDITY_LOW")
        checks.append("최근 20거래일 평균 거래대금과 체결 가능성을 다시 확인할 것")
    else:
        score += Decimal("15")

    if features.volume_ratio20 >= Decimal("2"):
        reasons.append("VOLUME_SURGE")
        score += Decimal("15")

    bottom_rebound = (
        features.drawdown60 <= Decimal("-0.20")
        and features.close > features.sma20
        and features.return5 > 0
    )
    momentum = (
        features.close > features.sma20 > features.sma60
        and features.return20 >= Decimal("0.05")
    )
    if bottom_rebound:
        reasons.append("BOTTOM_REBOUND")
        score += Decimal("20")
    if momentum:
        reasons.append("MOMENTUM_CONTINUATION")
        score += Decimal("20")
    if not bottom_rebound and not momentum:
        checks.append("바닥 반등 또는 상승 지속 가격 조건이 확인될 때까지 매수를 보류할 것")

    hard_exclusion = False
    if security.asset_type is AssetType.COMMON:
        if financing_data_available is False:
            warnings.append("DILUTION_HISTORY_UNAVAILABLE")
            checks.append("최근 5년 CB·BW·유상증자 공시 이력을 확인할 것")
        if management_data_available is False:
            warnings.append("MANAGEMENT_HISTORY_UNAVAILABLE")
            checks.append("최근 10년 경영진·대주주 공식 위험 이력을 확인할 것")
        if catalyst_data_available is False:
            warnings.append("CATALYST_HISTORY_UNAVAILABLE")
            checks.append("계약·실적 공시의 현재 유효성을 확인할 것")
        company_kind_unknown = security.company_kind is CompanyKind.UNKNOWN
        if company_kind_unknown:
            warnings.append("COMPANY_KIND_UNAVAILABLE")
            checks.append("금융회사 여부와 적용할 재무 기준을 확인할 것")
        if financial is None:
            warnings.append("DATA_UNAVAILABLE")
            checks.append("최신 연간·TTM 재무와 공시일을 확인할 것")
        else:
            assert_point_in_time(as_of, published_at=financial.published_at, label="financial")
            if financial.symbol != security.symbol:
                raise ValueError("financial symbol does not match security")
            if financial.operating_income <= 0:
                warnings.append("OPERATING_LOSS")
                hard_exclusion = True
            else:
                reasons.append("OPERATING_PROFIT_POSITIVE")
                score += Decimal("10")

            if security.company_kind is CompanyKind.FINANCIAL:
                reasons.append("FINANCIAL_SECTOR_SEPARATE")
                checks.append("금융회사 전용 건전성 지표로 별도 검토할 것")
            elif financial.operating_cash_flow is None:
                warnings.append("OCF_UNAVAILABLE")
                checks.append("영업활동현금흐름 양수 여부를 확인할 것")
            elif financial.operating_cash_flow <= 0:
                warnings.append("OCF_NON_POSITIVE")
                if not company_kind_unknown:
                    hard_exclusion = True
            else:
                reasons.append("OCF_POSITIVE")
                score += Decimal("10")

            if financial.free_cash_flow is not None and financial.free_cash_flow < 0:
                warnings.append("FCF_NEGATIVE_REVIEW")
                checks.append("FCF 음수가 성장투자·일회성 지출인지 구조적 부족인지 확인할 것")
            elif financial.free_cash_flow is not None:
                score += Decimal("5")

            receivable_trend = trend_is_stable_or_rising(financial.receivable_turnover)
            inventory_trend = trend_is_stable_or_rising(financial.inventory_turnover)
            if False in (receivable_trend, inventory_trend):
                warnings.append("TURNOVER_WEAKENING")
                checks.append("매출채권·재고자산 회전율 약화 원인을 확인할 것")
            elif receivable_trend is True and inventory_trend is True:
                score += Decimal("5")

    five_year_cutoff = as_of.astimezone(timezone.utc) - timedelta(days=365 * 5 + 2)
    dilution_count = 0
    dilution_ratios: list[Decimal] = []
    refixing_count = 0
    financing_purposes: set[str] = set()
    for event in financing_events:
        assert_point_in_time(as_of, published_at=event.announced_at, label="financing")
        if event.symbol != security.symbol:
            raise ValueError("financing symbol does not match security")
        if event.dilutive and event.official and event.announced_at >= five_year_cutoff:
            dilution_count += 1
            if event.dilution_ratio_pct is not None:
                dilution_ratios.append(event.dilution_ratio_pct)
            if event.refixing is True:
                refixing_count += 1
            if event.purpose:
                financing_purposes.add(event.purpose)
    if dilution_count:
        warnings.append("DILUTION_EVENT")
        checks.append("조달 목적·규모·주식수 증가와 반복성을 확인할 것")
        if len(dilution_ratios) < dilution_count:
            warnings.append("DILUTION_SCALE_UNAVAILABLE")
        if refixing_count:
            warnings.append("REFIXING_PRESENT")
            checks.append("리픽싱 최저가·조정 조건과 잠재 전환 물량을 확인할 것")
    if dilution_count >= 2:
        warnings.append("REPEATED_DILUTION")
        score = max(Decimal("0"), score - Decimal("20"))

    for risk in management_risks:
        if risk.symbol != security.symbol:
            raise ValueError("management risk symbol does not match security")
        for evidence in risk.evidence:
            assert_point_in_time(
                as_of,
                observed_at=evidence.observed_at,
                published_at=evidence.published_at,
                label="management risk evidence",
            )
        if risk.confirmed:
            warnings.append("MANAGEMENT_RISK_CONFIRMED")
            hard_exclusion = True
        else:
            warnings.append("MANAGEMENT_RISK_OFFICIAL_REVIEW")
            checks.append("공식 공시에 언급된 횡령·배임 위험의 수사·기소·판결 상태를 확인할 것")

    catalyst_score = Decimal("0")
    for catalyst in catalysts:
        if catalyst.symbol != security.symbol:
            raise ValueError("catalyst symbol does not match security")
        status = _current_catalyst_status(catalyst, as_of)
        if status is CatalystStatus.CONFIRMED:
            reasons.append("CATALYST_CONFIRMED")
            catalyst_score = max(catalyst_score, Decimal("15"))
        elif status is CatalystStatus.PARTIAL:
            reasons.append("CATALYST_PARTIAL")
            catalyst_score = max(catalyst_score, Decimal("7"))
            checks.append("비공식 정보와 연결된 공식 공시·실적·계약 근거를 추가 확인할 것")
        elif status is CatalystStatus.RUMOR_ONLY:
            warnings.append("RUMOR_ONLY")
            checks.append("공식 공시나 회사 발표 전까지 재료로 인정하지 말 것")
        else:
            warnings.append("CATALYST_EXPIRED")
    score = min(Decimal("100"), score + catalyst_score)

    if hard_exclusion:
        decision = Decision.EXCLUDED
    elif checks or warnings or not (bottom_rebound or momentum):
        decision = Decision.BUY_HOLD
    else:
        decision = Decision.CANDIDATE

    metrics = {
        "close": features.close,
        "sma20": features.sma20,
        "sma60": features.sma60,
        "return5": features.return5,
        "return20": features.return20,
        "drawdown60": features.drawdown60,
        "volume_ratio20": features.volume_ratio20,
        "average_value20": features.average_value20,
        "atr14": features.atr14,
        "dilution_count_5y": dilution_count,
        "max_dilution_ratio_pct_5y": max(dilution_ratios) if dilution_ratios else None,
        "refixing_event_count_5y": refixing_count,
        "financing_purposes_5y": " | ".join(sorted(financing_purposes)) or None,
    }
    return ScreeningResult(
        security.symbol,
        as_of,
        decision,
        score,
        tuple(dict.fromkeys(reasons)),
        tuple(dict.fromkeys(warnings)),
        tuple(dict.fromkeys(checks)),
        metrics,
        security.name,
    )


def select_top_candidates(results: list[ScreeningResult], limit: int = 5) -> list[ScreeningResult]:
    if limit < 1:
        raise ValueError("limit must be positive")
    eligible = [item for item in results if item.decision is not Decision.EXCLUDED]
    return sorted(eligible, key=lambda item: (-item.score, item.symbol))[:limit]
