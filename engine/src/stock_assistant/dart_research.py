"""Bounded, resumable DART collection across the ordinary-stock universe."""
from calendar import monthrange
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from time import sleep
from urllib.parse import parse_qs, urlsplit

from .financial_research import AnnualProfit, evaluate_financial
from .models import AssetType, CompanyKind, to_json_value
from .providers.dart import KST
from .providers.http import AuthenticationError, ProviderError, RateLimitError
from .repository import _evidence_from_dict, _financial_from_dict, _security_from_dict
from .validation import validate_analysis_time

_PERIOD = re.compile(r"\((20\d{2})[./-](\d{2})\)")


def receipt(url: str) -> str:
    parsed = urlsplit(url)
    value = parse_qs(parsed.query).get("rcpNo", [""])[0]
    if parsed.hostname != "dart.fss.or.kr" or not re.fullmatch(r"[0-9]{14}", value):
        raise ValueError("DART statement receipt is unavailable")
    return value


def _year_start(day: date, years: int) -> date:
    return date(day.year - years, day.month, min(day.day, monthrange(day.year - years, day.month)[1]))


def decode_bundle(payload: dict):
    security = _security_from_dict(payload["security"])
    snapshot = _financial_from_dict(payload["snapshot"]) if payload.get("snapshot") else None
    history = tuple(AnnualProfit(p["symbol"], date.fromisoformat(p["period_end"]),
                                 Decimal(p["operating_income"]),
                                 datetime.fromisoformat(p["published_at"]), datetime.fromisoformat(p["observed_at"]),
                                 p["receipt_id"], p["statement_basis"], p["source_url"])
                    for p in payload.get("annual_history", []))
    return security, snapshot, history


def stored_filings(store, symbol, *, as_of, cursor=None):
    merged = {e["url"]: e for e in (cursor["payload"].get("filings", []) if cursor else [])}
    for record in store.records("dart_filing", symbol, as_of=as_of):
        merged[record["payload"]["url"]] = record["payload"]
    return [_evidence_from_dict(e) for e in merged.values()]


class DartResearchCollector:
    def __init__(self, repository, store, client, *, clock=None, sleeper=sleep,
                 interval_seconds=0.1, request_limit=2000):
        if not 0 <= interval_seconds <= 10 or type(request_limit) is not int or not 1 <= request_limit <= 5000:
            raise ValueError("invalid DART collection bounds")
        self.repository, self.store, self.client = repository, store, client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper, self.interval, self.limit = sleeper, interval_seconds, request_limit
        self.requests = self.company_requests = 0

    def _call(self, method, **kwargs):
        if self.requests >= self.limit or self.company_requests >= 500:
            raise ValueError("DART_REQUEST_BUDGET_EXHAUSTED")
        self.requests += 1
        self.company_requests += 1
        try:
            return method(**kwargs)
        finally:
            self.sleeper(self.interval)

    def _filings(self, corp_code, start, end, observed):
        def window(first, last):
            page = self._call(self.client.filing_page, corp_code=corp_code,
                              begin_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"),
                              observed_at=observed, page_no=1, page_count=100)
            if page.total_page > 50:
                if first == last:
                    raise ValueError("DART_SINGLE_DAY_PAGE_LIMIT")
                middle = first + (last - first) // 2
                return window(first, middle) + window(middle + timedelta(days=1), last)
            pages = [page]
            for number in range(2, page.total_page + 1):
                following = self._call(self.client.filing_page, corp_code=corp_code,
                                       begin_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"),
                                       observed_at=observed, page_no=number, page_count=100)
                if following.page_no != number or following.total_page != page.total_page:
                    raise ValueError("DART_PAGINATION_CHANGED")
                pages.append(following)
            # A failed page never advances the coverage cursor.
            return [replace(e, observed_at=max(e.observed_at, self.clock())) for p in pages for e in p.filings]
        return window(start, end)

    def run(self, *, symbols=None, batch_size=25, force=False, priority_symbols=None):
        if type(batch_size) is not int or not 1 <= batch_size <= 50:
            raise ValueError("DART batch size must be between 1 and 50")
        now = validate_analysis_time(self.clock())
        all_securities = {s.symbol: s for s in self.repository.list_securities()}
        if symbols is not None and (not symbols or len(symbols) > 50 or len(set(symbols)) != len(symbols)
                                    or any(s not in all_securities for s in symbols)):
            raise ValueError("symbols must contain 1 to 50 unique known securities")
        eligible = [s for s in all_securities.values() if s.asset_type is AssetType.COMMON
                    and (not s.listed_on or s.listed_on <= now.astimezone(KST).date())
                    and (not s.delisted_on or s.delisted_on > now.astimezone(KST).date())
                    and (symbols is None or s.symbol in symbols)]
        attempts = self.store.latest_by_symbol("dart_attempt", as_of=now)
        cursors = self.store.latest_by_symbol("dart_cursor", as_of=now)
        eligible.sort(key=lambda s: ((attempts.get(s.symbol) or {}).get("observed_at", ""), s.symbol))
        selected = []
        end = now.astimezone(KST).date() - timedelta(days=1)
        priority_set = set(priority_symbols or ())
        if priority_set and symbols is None:
            priority_order = {sym: idx for idx, sym in enumerate(priority_symbols)}
            priority_due = []
            for security in eligible:
                if security.symbol not in priority_set:
                    continue
                cursor, attempt = cursors.get(security.symbol), attempts.get(security.symbol)
                failed = attempt and attempt["payload"].get("status") == "FAILED"
                if not force and not failed and cursor and date.fromisoformat(cursor["payload"]["end"]) >= end:
                    continue
                priority_due.append(security)
            priority_due.sort(key=lambda s: priority_order[s.symbol])
            selected.extend(priority_due[:batch_size])
        selected_symbols = {s.symbol for s in selected}
        for security in eligible:
            if len(selected) >= batch_size:
                break
            if security.symbol in selected_symbols:
                continue
            cursor, attempt = cursors.get(security.symbol), attempts.get(security.symbol)
            failed = attempt and attempt["payload"].get("status") == "FAILED"
            if not force and not failed and cursor and date.fromisoformat(cursor["payload"]["end"]) >= end:
                continue
            selected.append(security)
            selected_symbols.add(security.symbol)
        if not selected:
            return {"selected": 0, "requests": 0, "results": [], "source_read_only": True}
        if symbols is None and not force and not priority_set:
            # Keep issuers under active second-stage review current, without exhausting the rotation batch.
            followed=set(self.store.latest_by_symbol('news_coverage',as_of=now))
            focus = self.store.latest('research_focus', 'ALL', as_of=now)
            if focus:followed.update(focus['payload'].get('symbols', []))
            followed.update(self.store.latest_by_symbol('recent_news_coverage',as_of=now))
            followed.update(self.store.latest_by_symbol('recent_archive_cursor',as_of=now))
            followed.update(s for s,r in self.store.latest_by_symbol('archive_cursor',as_of=now).items()
                            if r['payload'].get('status') in {'PARTIAL','COMPLETE'})
            review_targets=[]
            for security in eligible:
                cursor=cursors.get(security.symbol)
                if security.symbol in followed and cursor and date.fromisoformat(cursor['payload']['end'])<end:
                    review_targets.append(security)
                    if len(review_targets)==min(5,batch_size):break
            selected=(review_targets+[s for s in selected if s.symbol not in {r.symbol for r in review_targets}])[:batch_size]
        codes = self._call(self.client.corp_codes)
        results = []
        for security in selected:
            self.company_requests = 0
            result = {"symbol": security.symbol}
            try:
                code = codes.get(security.symbol)
                if not code:
                    raise ValueError("DART_CORP_CODE_UNAVAILABLE")
                cached_profile = self.store.latest("dart_profile", security.symbol, as_of=now)
                if cached_profile and now - datetime.fromisoformat(cached_profile["observed_at"]) < timedelta(days=7):
                    profile = cached_profile["payload"]
                else:
                    p = self._call(self.client.company_profile, corp_code=code)
                    profile = {"kind": p.kind.value, "fiscal_month": p.fiscal_month}
                    self.store.put("dart_profile", security.symbol, profile, observed_at=self.clock())
                security = replace(security, company_kind=CompanyKind(profile["kind"]))
                if security.company_kind is not CompanyKind.GENERAL or profile["fiscal_month"] != 12:
                    self.store.put("financial_bundle", security.symbol,
                                   {"security": to_json_value(security), "snapshot": None, "annual_history": [],
                                    "status": "SEPARATE_POLICY_REQUIRED" if security.company_kind is CompanyKind.FINANCIAL
                                    else "CLASSIFICATION_OR_FISCAL_PERIOD_REVIEW_REQUIRED"}, observed_at=self.clock())
                    result["status"] = "SEPARATE_POLICY_REQUIRED"
                else:
                    result.update(self._company(security, code, now, end, force))
            except (AuthenticationError, RateLimitError) as exc:
                result.update(status="FAILED", error_code=exc.code)
                results.append(result)
                self.store.put("dart_attempt", security.symbol, result, observed_at=self.clock())
                break
            except (ProviderError, ValueError) as exc:
                result.update(status="FAILED", error_code=exc.code if isinstance(exc, ProviderError) else str(exc))
            self.store.put("dart_attempt", security.symbol, result, observed_at=self.clock())
            results.append(result)
            if self.requests >= self.limit:
                break
        return {"universe_size": len(eligible), "selected": len(selected), "processed": len(results),
                "requests": self.requests, "results": results, "source_read_only": True}

    def _company(self, security, code, now, end, force):
        cursor = self.store.latest("dart_cursor", security.symbol, as_of=now)
        required_start = _year_start(end, 10)
        start = max(required_start, date.fromisoformat(cursor["payload"]["end"]) - timedelta(days=7)) if cursor else required_start
        filings = self._filings(code, start, end, now)
        previous = stored_filings(self.store, security.symbol, as_of=now, cursor=cursor)
        merged = {e.url: to_json_value(e) for e in previous}
        changed = [("dart_filing", security.symbol, e) for e in cursor["payload"].get("filings", [])] if cursor else []
        # Preserve first observation for identical receipts; changed metadata is a new version.
        for e in filings:
            p = to_json_value(e)
            if p["url"] in merged and {k: v for k, v in p.items() if k != "observed_at"} == {
                    k: v for k, v in merged[p["url"]].items() if k != "observed_at"}:
                continue
            merged[p["url"]] = p
            changed.append(("dart_filing", security.symbol, p))
        self.store.put_many(changed, observed_at=self.clock())
        all_filings = [_evidence_from_dict(e) for e in merged.values()]
        periods = {}
        for filing in all_filings:
            match = _PERIOD.search(filing.title)
            if not match or filing.published_at > now:
                continue
            year, month = map(int, match.groups())
            code_report = "11011" if "사업보고서" in filing.title and month == 12 else {
                3: "11013", 6: "11012", 9: "11014"}.get(month) if (
                "분기보고서" in filing.title or "반기보고서" in filing.title) else None
            if not code_report or year < 2015:
                continue
            key = (year, month, code_report)
            if key not in periods or filing.published_at > periods[key].published_at:
                periods[key] = filing
        annual_keys = sorted((k for k in periods if k[2] == "11011"), reverse=True)[:3]
        if not annual_keys:
            raise ValueError("DART_ANNUAL_FILING_UNAVAILABLE")
        latest_annual = annual_keys[0]
        interim_keys = sorted((k for k in periods if k[0] == latest_annual[0] + 1), reverse=True)
        interim_key = interim_keys[0] if interim_keys else None
        receipts = {receipt(e.url): e for e in all_filings}
        chosen = None
        for basis in ("CFS", "OFS"):
            snapshots = []
            for key in reversed(annual_keys):
                desired_receipt = receipt(periods[key].url)
                cache_symbol = f"{security.symbol}:{key[0]}:{key[2]}:{basis}:{desired_receipt}"
                cached = self.store.latest("dart_statement", cache_symbol, as_of=now)
                if cached and not force:
                    snap = _financial_from_dict(cached["payload"]["snapshot"])
                    observed = datetime.fromisoformat(cached["observed_at"])
                else:
                    snap = self._call(self.client.financial_statement, corp_code=code, symbol=security.symbol,
                                      business_year=key[0], report_code=key[2], financial_statement_division=basis,
                                      period_end=date(key[0], 12, 31), published_at=periods[key].published_at)
                    observed = self.clock()
                    if snap is None:
                        break
                    actual = receipts.get(receipt(snap.source_url))
                    if actual is None or receipt(snap.source_url) != desired_receipt:
                        raise ValueError("DART_STATEMENT_RECEIPT_MISMATCH")
                    snap = replace(snap, published_at=actual.published_at)
                    self.store.put("dart_statement", cache_symbol, {"snapshot": to_json_value(snap)}, observed_at=observed)
                snapshots.append((snap, observed))
            if len(snapshots) != len(annual_keys):
                continue
            annual = snapshots[-1][0]
            if interim_key:
                key = interim_key
                desired = receipt(periods[key].url)
                cache_symbol = f"{security.symbol}:{key[0]}:{key[2]}:{basis}:{desired}"
                cached = self.store.latest("dart_interim", cache_symbol, as_of=now)
                if cached and not force:
                    values = cached["payload"]
                else:
                    ttm = self._call(self.client.operating_income_periods, corp_code=code, business_year=key[0],
                                     report_code=key[2], financial_statement_division=basis)
                    if ttm is None:
                        continue  # Restart the entire lineage as OFS; never mix bases.
                    if receipt(ttm.source_url) != desired:
                        raise ValueError("DART_INTERIM_RECEIPT_MISMATCH")
                    values = to_json_value(ttm)
                    self.store.put("dart_interim", cache_symbol, values, observed_at=self.clock())
                income = annual.operating_income + Decimal(values["current_cumulative"]) - Decimal(values["previous_cumulative"])
                snapshot = replace(annual, operating_income=income, annual_operating_income=annual.operating_income,
                                   ttm_operating_income=income, ttm_period_end=date(key[0], key[1], monthrange(key[0], key[1])[1]),
                                   ttm_source_url=values["source_url"], published_at=max(annual.published_at, periods[key].published_at))
            else:
                snapshot = replace(annual, annual_operating_income=annual.operating_income,
                                   ttm_operating_income=annual.operating_income, ttm_period_end=annual.period_end,
                                   ttm_source_url=annual.source_url)
            chosen = snapshot, snapshots, basis
            break
        if chosen is None:
            raise ValueError("DART_COMPARABLE_STATEMENTS_UNAVAILABLE")
        snapshot, snapshots, basis = chosen
        history = tuple(AnnualProfit(s.symbol, s.period_end, s.operating_income, s.published_at, observed,
                                    receipt(s.source_url), basis, s.source_url) for s, observed in snapshots)
        observed = self.clock()
        bundle = {"security": to_json_value(security), "snapshot": to_json_value(snapshot),
                  "annual_history": to_json_value(history), "statement_basis": basis,
                  "status": "COLLECTED", "historical_replay_certified": False}
        self.store.put("financial_bundle", security.symbol, bundle, observed_at=observed)
        filing_hash = hashlib.sha256(json.dumps(sorted(merged.values(), key=lambda e: e["url"]), sort_keys=True).encode()).hexdigest()
        self.store.put("dart_cursor", security.symbol, {"start": required_start.isoformat(), "end": end.isoformat(),
                                                        "filings_fingerprint": filing_hash, "filing_count": len(merged)}, observed_at=observed)
        review = evaluate_financial(security, snapshot, as_of=observed, annual_history=history)
        return {"status": "COLLECTED", "gate": review.gate, "annual_years": len(history),
                "statement_basis": basis, "financial_score": to_json_value(review.financial_score),
                "pending_checks": list(review.pending_checks)}
