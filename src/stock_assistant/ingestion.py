from __future__ import annotations

from dataclasses import dataclass
from calendar import monthrange
from datetime import date, datetime, timedelta
from time import sleep
from typing import Callable

from .disclosures import classify_dart_filing_signals
from .models import AssetType, CompanyKind, Evidence, Market, Security
from .providers.dart import DART_FINANCING_ENDPOINTS, KST, DartClient
from .providers.krx import KrxClient
from .repository import StockRepository


@dataclass(frozen=True, slots=True)
class KrxIngestionSummary:
    requested_weekdays: int
    trading_days_with_data: int
    securities_saved: int
    bars_saved: int
    latest_trading_date: date | None


class KrxHistoryIngestor:
    def __init__(
        self,
        repository: StockRepository,
        client: KrxClient,
        *,
        request_interval_seconds: float = 0.1,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.repository = repository
        self.client = client
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        self.request_interval_seconds = request_interval_seconds
        self.sleeper = sleeper

    def _pace(self) -> None:
        if self.request_interval_seconds:
            self.sleeper(self.request_interval_seconds)

    def ingest_day(self, *, business_date: date, observed_at: datetime) -> KrxIngestionSummary:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if business_date.weekday() >= 5:
            return KrxIngestionSummary(0, 0, 0, 0, None)
        bars_saved = 0
        day_has_equity = False
        day_has_data = False
        etfs: list[Security] = []
        for market in ("KOSPI", "KOSDAQ", "ETF"):
            snapshot = self.client.daily_snapshot(market, business_date, observed_at=observed_at)
            self._pace()
            bars = list(snapshot.bars)
            self.repository.save_bars(bars)
            bars_saved += len(bars)
            day_has_data = day_has_data or bool(bars)
            if market in {"KOSPI", "KOSDAQ"} and bars:
                day_has_equity = True
            if market == "ETF":
                for bar in bars:
                    name = snapshot.names.get(bar.symbol)
                    if name:
                        etfs.append(Security(
                            bar.symbol, name, Market.KOSPI,
                            AssetType.ETF, CompanyKind.FUND, None,
                        ))
        securities: list[Security] = []
        if day_has_equity:
            securities.extend(self.client.securities("KOSPI", business_date))
            self._pace()
            securities.extend(self.client.securities("KOSDAQ", business_date))
            self._pace()
        securities.extend(etfs)
        self.repository.save_securities(securities)
        return KrxIngestionSummary(
            requested_weekdays=1 if business_date.weekday() < 5 else 0,
            trading_days_with_data=1 if day_has_data else 0,
            securities_saved=len(securities),
            bars_saved=bars_saved,
            latest_trading_date=business_date if day_has_data else None,
        )

    def backfill(
        self,
        *,
        end_date: date,
        calendar_days: int,
        observed_at: datetime,
    ) -> KrxIngestionSummary:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if calendar_days < 90 or calendar_days > 366:
            raise ValueError("calendar_days must be between 90 and 366")
        dates = [
            end_date - timedelta(days=offset)
            for offset in reversed(range(calendar_days))
            if (end_date - timedelta(days=offset)).weekday() < 5
        ]
        bars_saved = 0
        trading_days: set[date] = set()
        etfs: dict[str, Security] = {}
        latest_equity_date: date | None = None
        for business_date in dates:
            day_has_equity = False
            for market in ("KOSPI", "KOSDAQ", "ETF"):
                snapshot = self.client.daily_snapshot(market, business_date, observed_at=observed_at)
                self._pace()
                bars = list(snapshot.bars)
                self.repository.save_bars(bars)
                bars_saved += len(bars)
                if bars:
                    trading_days.add(business_date)
                if market in {"KOSPI", "KOSDAQ"} and bars:
                    day_has_equity = True
                if market == "ETF":
                    for bar in bars:
                        name = snapshot.names.get(bar.symbol)
                        if name:
                            etfs[bar.symbol] = Security(
                                bar.symbol,
                                name,
                                Market.KOSPI,
                                AssetType.ETF,
                                CompanyKind.FUND,
                                None,
                            )
            if day_has_equity:
                latest_equity_date = business_date

        securities: list[Security] = []
        if latest_equity_date is not None:
            securities.extend(self.client.securities("KOSPI", latest_equity_date))
            self._pace()
            securities.extend(self.client.securities("KOSDAQ", latest_equity_date))
            self._pace()
        securities.extend(etfs.values())
        self.repository.save_securities(securities)
        return KrxIngestionSummary(
            requested_weekdays=len(dates),
            trading_days_with_data=len(trading_days),
            securities_saved=len(securities),
            bars_saved=bars_saved,
            latest_trading_date=max(trading_days) if trading_days else None,
        )


@dataclass(frozen=True, slots=True)
class DartEnrichmentSummary:
    requested: int
    enriched: int
    missing_corp_code: int
    missing_annual_filing: int
    missing_statement: int
    unknown_company_kind: int
    already_current: int


class DartFinancialEnricher:
    def __init__(
        self,
        repository: StockRepository,
        client: DartClient,
        *,
        request_interval_seconds: float = 0.2,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.repository = repository
        self.client = client
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        self.request_interval_seconds = request_interval_seconds
        self.sleeper = sleeper

    def _pace(self) -> None:
        if self.request_interval_seconds:
            self.sleeper(self.request_interval_seconds)

    def enrich(
        self,
        *,
        symbols: list[str],
        as_of: datetime,
        business_year: int,
    ) -> DartEnrichmentSummary:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols or len(unique_symbols) > 50:
            raise ValueError("symbols must contain between 1 and 50 unique items")
        if business_year < 2015 or business_year > as_of.year:
            raise ValueError("business_year is outside the supported DART range")
        securities = {item.symbol: item for item in self.repository.list_securities()}
        pending: list[str] = []
        already_current = 0
        for symbol in unique_symbols:
            security = securities.get(symbol)
            existing = self.repository.latest_financial(symbol, as_of=as_of)
            if (
                security is not None
                and security.asset_type is AssetType.COMMON
                and security.company_kind is not CompanyKind.UNKNOWN
                and existing is not None
                and existing.period_end.year >= business_year
            ):
                already_current += 1
            else:
                pending.append(symbol)
        if not pending:
            return DartEnrichmentSummary(
                requested=len(unique_symbols), enriched=0, missing_corp_code=0,
                missing_annual_filing=0, missing_statement=0,
                unknown_company_kind=0, already_current=already_current,
            )
        corp_codes = self.client.corp_codes()
        self._pace()
        enriched = 0
        missing_corp_code = 0
        missing_annual_filing = 0
        missing_statement = 0
        unknown_company_kind = 0
        for symbol in pending:
            security = securities.get(symbol)
            if security is None or security.asset_type is not AssetType.COMMON:
                missing_corp_code += 1
                continue
            corp_code = corp_codes.get(symbol)
            if corp_code is None:
                missing_corp_code += 1
                continue
            profile = self.client.company_profile(corp_code)
            self._pace()
            updated = Security(
                security.symbol,
                security.name,
                security.market,
                security.asset_type,
                profile.kind,
                security.listed_on,
                security.delisted_on,
            )
            self.repository.save_securities([updated])
            if profile.kind is CompanyKind.UNKNOWN or profile.fiscal_month is None:
                unknown_company_kind += 1
                continue
            filing_start = date(business_year + 1, 1, 1)
            fully_observable_end = as_of.astimezone(KST).date() - timedelta(days=1)
            if fully_observable_end < filing_start:
                missing_annual_filing += 1
                continue
            filings = self.client.filings(
                observed_at=as_of,
                corp_code=corp_code,
                begin_date=filing_start.strftime("%Y%m%d"),
                end_date=fully_observable_end.strftime("%Y%m%d"),
                page_count=100,
            )
            self._pace()
            annual = [item for item in filings if "사업보고서" in item.title and item.published_at <= as_of]
            if not annual:
                missing_annual_filing += 1
                continue
            published_at = max(item.published_at for item in annual)
            period_end = date(business_year, profile.fiscal_month, monthrange(business_year, profile.fiscal_month)[1])
            snapshot = self.client.financial_statement(
                corp_code=corp_code,
                symbol=symbol,
                business_year=business_year,
                report_code="11011",
                financial_statement_division="CFS",
                period_end=period_end,
                published_at=published_at,
            )
            self._pace()
            if snapshot is None:
                snapshot = self.client.financial_statement(
                    corp_code=corp_code,
                    symbol=symbol,
                    business_year=business_year,
                    report_code="11011",
                    financial_statement_division="OFS",
                    period_end=period_end,
                    published_at=published_at,
                )
                self._pace()
            if snapshot is None:
                missing_statement += 1
                continue
            self.repository.save_financial_snapshot(snapshot)
            enriched += 1
        return DartEnrichmentSummary(
            requested=len(unique_symbols),
            enriched=enriched,
            missing_corp_code=missing_corp_code,
            missing_annual_filing=missing_annual_filing,
            missing_statement=missing_statement,
            unknown_company_kind=unknown_company_kind,
            already_current=already_current,
        )


@dataclass(frozen=True, slots=True)
class DartDisclosureSummary:
    requested: int
    covered: int
    events_saved: int
    catalysts_saved: int
    management_risks_saved: int
    missing_corp_code: int
    skipped_non_common: int


class DartDisclosureEnricher:
    """Persist official five-year dilution history without treating missing data as no events."""

    def __init__(
        self,
        repository: StockRepository,
        client: DartClient,
        *,
        request_interval_seconds: float = 0.2,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.repository = repository
        self.client = client
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        self.request_interval_seconds = request_interval_seconds
        self.sleeper = sleeper

    def _pace(self) -> None:
        if self.request_interval_seconds:
            self.sleeper(self.request_interval_seconds)

    def _receipt_dates(
        self,
        *,
        corp_code: str,
        start_date: date,
        end_date: date,
        observed_at: datetime,
    ) -> tuple[dict[str, datetime], list[Evidence]]:
        receipt_dates: dict[str, datetime] = {}
        filings: list[Evidence] = []
        page_no = 1
        total_page = 1
        while page_no <= total_page:
            page = self.client.filing_page(
                observed_at=observed_at,
                corp_code=corp_code,
                begin_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                page_no=page_no,
                page_count=100,
            )
            self._pace()
            if page.total_page and page.page_no != page_no:
                raise ValueError("DART filing response page number does not match the request")
            if page.total_page > 50:
                raise ValueError("DART filing history exceeds the 50-page safety limit")
            total_page = page.total_page
            for receipt, published_at in page.receipt_dates.items():
                existing = receipt_dates.get(receipt)
                if existing is not None and existing != published_at:
                    raise ValueError(f"DART filing receipt date changed across pages: {receipt}")
                receipt_dates[receipt] = published_at
            filings.extend(page.filings)
            if total_page == 0:
                break
            page_no += 1
        return receipt_dates, filings

    def enrich(self, *, symbols: list[str], as_of: datetime) -> DartDisclosureSummary:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols or len(unique_symbols) > 50:
            raise ValueError("symbols must contain between 1 and 50 unique items")
        end_date = as_of.astimezone(KST).date() - timedelta(days=1)
        financing_start = max(end_date - timedelta(days=365 * 5 + 2), date(2015, 1, 1))
        management_start = max(end_date - timedelta(days=365 * 10 + 3), date(2015, 1, 1))
        securities = {item.symbol: item for item in self.repository.list_securities()}
        corp_codes = self.client.corp_codes()
        self._pace()
        covered = 0
        events_saved = 0
        catalysts_saved = 0
        management_risks_saved = 0
        missing_corp_code = 0
        skipped_non_common = 0
        for symbol in unique_symbols:
            security = securities.get(symbol)
            if security is None or security.asset_type is not AssetType.COMMON:
                skipped_non_common += 1
                continue
            corp_code = corp_codes.get(symbol)
            if corp_code is None:
                missing_corp_code += 1
                continue
            receipt_dates, filings = self._receipt_dates(
                corp_code=corp_code,
                start_date=management_start,
                end_date=end_date,
                observed_at=as_of,
            )
            events = []
            for event_type in DART_FINANCING_ENDPOINTS:
                events.extend(self.client.financing_events(
                    corp_code=corp_code,
                    symbol=symbol,
                    event_type=event_type,
                    begin_date=financing_start.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    observed_at=as_of,
                    receipt_dates=receipt_dates,
                ))
                self._pace()
            self.repository.save_financing_events(events)
            catalysts, management_risks = classify_dart_filing_signals(
                symbol,
                filings,
                as_of=as_of,
            )
            self.repository.save_catalysts(catalysts)
            self.repository.save_management_risks(management_risks)
            self.repository.save_coverage(
                symbol=symbol,
                dataset="DART_FINANCING",
                start_date=financing_start.isoformat(),
                end_date=end_date.isoformat(),
                observed_at=as_of,
            )
            for dataset in ("DART_CATALYST", "DART_MANAGEMENT_RISK"):
                self.repository.save_coverage(
                    symbol=symbol,
                    dataset=dataset,
                    start_date=management_start.isoformat(),
                    end_date=end_date.isoformat(),
                    observed_at=as_of,
                )
            covered += 1
            events_saved += len(events)
            catalysts_saved += len(catalysts)
            management_risks_saved += len(management_risks)
        return DartDisclosureSummary(
            requested=len(unique_symbols),
            covered=covered,
            events_saved=events_saved,
            catalysts_saved=catalysts_saved,
            management_risks_saved=management_risks_saved,
            missing_corp_code=missing_corp_code,
            skipped_non_common=skipped_non_common,
        )
