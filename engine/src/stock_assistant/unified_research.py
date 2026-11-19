"""Opt-in financial -> news -> total -> conditional buy recommendations."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from collections import Counter

from .dart_research import decode_bundle, stored_filings
from .financial_research import evaluate_financial
from .indicators import InsufficientHistory, price_features
from .models import to_json_value
from .news_research import event_from_dict, score_news
from .news_policy import news_windows
from .risk_history import risk_coverage_payload
from .repository import _evidence_from_dict
from .validation import assert_point_in_time, validate_analysis_time

POLICY_VERSION = "unified-research-2026-10-14.1"
POLICY_VERSION_ACTIONABLE = "unified-research-2026-11-18.1"
KST = timezone(timedelta(hours=9))
LABELS = {"BUY_RECOMMENDATION": "매수 추천 (다음 거래일 조건부)", "WATCH": "관찰",
          "VERIFY_PENDING": "확인 대기", "EXCLUDED": "제외", "NOT_APPLICABLE": "별도 평가 필요"}
STRATEGY_LABELS = {
    "BOTTOM_REBOUND": "바닥 반등",
    "MOMENTUM_CONTINUATION": "모멘텀 지속",
    "WAIT_FOR_SETUP": "셋업 대기",
}
_RISK_CATEGORY_LABELS = {
    "MANAGEMENT": "경영·감사(횡령/배임/감사의견)",
    "DILUTION": "희석(유상증자/CB/BW)",
    "BUSINESS_DAMAGE": "사업 훼손(계약해지/거래정지/회생)",
}
_ADVISORY_LABELS = {
    "TURNOVER_WEAKENING": "회전율 둔화",
    "FCF_NEGATIVE_GROWTH_INVESTMENT": "음수 FCF(성장투자 확인)",
    "FCF_UNAVAILABLE_OPTIONAL": "FCF 미제공",
    "SEPARATE_ASSET_POLICY_REQUIRED": "별도 자산군 정책 대상",
}
_RANK_BADGES = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")


def price_review(symbol, bars, *, as_of, expected_session=None, verified_close_at=None):
    for bar in bars:
        if bar.symbol != symbol or bar.trade_date > as_of.astimezone(KST).date():
            raise ValueError("invalid price issuer or future session")
        assert_point_in_time(as_of, observed_at=bar.observed_at, label="research OHLCV")
    if expected_session is None:
        return {"status": "VERIFY_PENDING", "reasons": ["VERIFIED_MARKET_SESSION_UNAVAILABLE"]}
    if expected_session > as_of.astimezone(KST).date():
        raise ValueError("conditional recommendation cannot use a future session")
    if expected_session == as_of.astimezone(KST).date() and (
            verified_close_at is None or verified_close_at.tzinfo is None or verified_close_at > as_of
            or verified_close_at.astimezone(KST).date() != expected_session):
        return {"status": "VERIFY_PENDING", "reasons": ["MARKET_SESSION_CLOSE_UNVERIFIED"]}
    if not bars or max(b.trade_date for b in bars) != expected_session:
        return {"status": "VERIFY_PENDING", "reasons": ["PRICE_SESSION_INCOMPLETE"]}
    try:
        p = price_features(bars)
    except InsufficientHistory:
        return {"status": "VERIFY_PENDING", "reasons": ["PRICE_HISTORY_UNAVAILABLE"]}
    bottom = p.drawdown60 <= Decimal("-0.20") and p.close > p.sma20 and p.return5 > 0
    momentum = p.close > p.sma20 > p.sma60 and p.return20 >= Decimal("0.05")
    reasons = []
    if p.average_value20 < Decimal("1000000000"):
        reasons.append("LIQUIDITY_LOW")
    if not (bottom or momentum):
        reasons.append("PRICE_SETUP_UNAVAILABLE")
    return {"status": "WATCH" if reasons else "READY", "reasons": reasons,
            "strategy": "BOTTOM_REBOUND" if bottom else "MOMENTUM_CONTINUATION" if momentum else "WAIT_FOR_SETUP",
            "session": expected_session.isoformat(), "metrics": to_json_value(asdict(p)),
            "entry_conditions": ["다음 거래일 최신 시세로 유동성과 가격 조건을 다시 확인",
                                 "가격이 20일 이동평균 아래로 이탈하면 추천 무효", "새 악재·정정·취소가 확인되면 추천 무효"]}


def evaluate_unified(financial, news, price, *, provenance_current):
    if financial.as_of.tzinfo is None:
        raise ValueError("financial evaluation time is required")
    f, n, risk = financial.financial_score, news["news_score"], news["risk_penalty"]
    total = max(Decimal("0"), min(Decimal("100"), Decimal("0.7") * f + Decimal("0.3") * n - risk)) if (
        f is not None and n is not None) else None
    pending = list(financial.pending_checks) + list(news["pending_checks"])
    if not provenance_current:
        pending.append("DART_COLLECTION_NOT_CURRENT")
    if price["status"] == "VERIFY_PENDING":
        pending.extend(price["reasons"])
    reasons = list(financial.blocking_reasons) + list(news["hard_exclusions"])
    if financial.gate == "EXCLUDED" or news["hard_exclusions"]:
        decision = "EXCLUDED"
    elif financial.gate == "NOT_APPLICABLE":
        decision = "NOT_APPLICABLE"
    elif financial.gate != "PASS" or pending or total is None:
        decision = "VERIFY_PENDING"
    elif f < 70 or total < 75 or price["status"] != "READY":
        decision = "WATCH"
        reasons += ["FINANCIAL_SCORE_BELOW_THRESHOLD"] if f < 70 else []
        reasons += ["TOTAL_SCORE_BELOW_THRESHOLD"] if total < 75 else []
        reasons += price["reasons"]
    else:
        decision = "BUY_RECOMMENDATION"
    return {"symbol": financial.symbol, "name": financial.name, "action_decision": decision,
            "label": LABELS[decision], "financial_score": f, "news_score": n, "risk_penalty": risk, "total_score": total,
            "financial_breakdown": financial.score_breakdown, "news_events": news["events"], "price": price,
            "reasons": reasons, "pending_checks": list(dict.fromkeys(pending)), "advisories": list(financial.advisories),
            "financial_evaluation_id": financial.evaluation_id,
            'recent_news_complete': news.get('recent_coverage_complete', False),
            'risk_history_complete': news.get('risk_coverage_complete', False)}


def financial_universe(repository, store, *, as_of):
    results = []
    bundles = store.latest_by_symbol("financial_bundle", as_of=as_of)
    for security in repository.list_securities():
        bundle = bundles.get(security.symbol)
        history = ()
        if bundle:
            collected_security, snapshot, history = decode_bundle(bundle["payload"])
            security = replace(security, company_kind=collected_security.company_kind)
        else:
            snapshot = repository.latest_financial(security.symbol, as_of=as_of)
        growth = tuple(_evidence_from_dict(r["payload"]) for r in store.records("fcf_review", security.symbol, as_of=as_of))
        review = evaluate_financial(security, snapshot, as_of=as_of, annual_history=history, fcf_growth_evidence=growth)
        if bundle and bundle["payload"].get("status") == "COLLECTED":
            review = replace(review, advisories=tuple(a for a in review.advisories
                             if a != "LEGACY_FINANCIAL_AVAILABILITY_AND_REVISION_UNCERTIFIED"))
        results.append((security, review, bundle))
    return results


def _actionable_news_score(news: dict, snapshot: dict | None, *, as_of: datetime) -> tuple[Decimal, list[str]]:
    """Derive a transparent news score even when 90-day exhaustive pagination is incomplete."""
    warnings: list[str] = []
    if news["news_score"] is not None:
        return news["news_score"], warnings
    pos = sum(int(e["points"]) for e in news.get("events", []) if e.get("status") == "VERIFIED_ACTIVE" and e.get("points", 0) > 0)
    neg = sum(abs(int(e["points"])) for e in news.get("events", [])
              if e.get("status") == "ACTIVE_RISK" and e.get("score_target") == "news_score")
    score = Decimal(max(0, min(100, 50 + min(50, pos) - min(50, neg))))
    if snapshot and as_of - datetime.fromisoformat(snapshot["observed_at"]) <= timedelta(days=7):
        if snapshot["payload"].get("status") != "COMPLETE":
            warnings.append("뉴스 최근 100건 스냅샷 기준(90일 전체 미도달)")
    else:
        warnings.append("⚠️ 확인 필요: 최근 뉴스 미수집(중립 50점 기준)")
    return score, warnings


def _risk_warning_tags(news: dict) -> list[str]:
    counts = Counter(sig["category"] for sig in news.get("review_signals", []))
    tags = []
    for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        label = _RISK_CATEGORY_LABELS.get(category, category)
        tags.append(f"⚠️ 확인 필요: {label} 관련 기사/공시 {count}건")
    return tags


def evaluate_actionable(
    financial,
    news: dict,
    price: dict,
    *,
    as_of: datetime,
    dart_fresh: bool,
    news_snapshot: dict | None = None,
    price_fallback_session: date | None = None,
    tie_breaker: Decimal | None = None,
    liquidity_tier: str | None = None,
    in_cooldown: bool = False,
) -> dict:
    """Actionable evaluation: preserve hard exclusions, turn soft pending checks into warnings."""
    if financial.as_of.tzinfo is None:
        raise ValueError("financial evaluation time is required")
    reasons = list(financial.blocking_reasons) + list(news["hard_exclusions"])
    warnings: list[str] = []
    pending = list(financial.pending_checks) + list(news["pending_checks"])
    if not dart_fresh:
        pending.append("DART_COLLECTION_NOT_CURRENT")

    if financial.gate == "EXCLUDED" or news["hard_exclusions"]:
        decision = "EXCLUDED"
        n_score = news["news_score"]
        total = None
    elif financial.gate == "NOT_APPLICABLE":
        decision = "NOT_APPLICABLE"
        n_score = news["news_score"]
        total = None
    elif financial.gate != "PASS" or financial.financial_score is None:
        decision = "VERIFY_PENDING"
        n_score = news["news_score"]
        total = None
    else:
        n_score, news_warnings = _actionable_news_score(news, news_snapshot, as_of=as_of)
        warnings.extend(news_warnings)
        warnings.extend(_risk_warning_tags(news))
        if not dart_fresh:
            warnings.append("⚠️ 확인 필요: DART 공시 7일 이상 미갱신")
        if "FCF_CAUSE_REVIEW_REQUIRED" in financial.pending_checks:
            warnings.append("⚠️ 확인 필요: 음수 FCF 원인 검토")
        if "TURNOVER_HISTORY_UNAVAILABLE" in financial.pending_checks:
            warnings.append("⚠️ 확인 필요: 회전율 이력 부족")
        if price_fallback_session is not None:
            warnings.append(f"⚠️ 가격 기준: 직전 확보 거래일({price_fallback_session.isoformat()}) · 당일 KRX 일봉 미발행")
        for check in news["pending_checks"]:
            if check.startswith("UNRESOLVED_NEGATIVE_EVENT:"):
                warnings.append(f"⚠️ 확인 필요: 미해소 위험 사건({check.split(':', 1)[1]})")

        f = financial.financial_score
        risk = news["risk_penalty"]
        total = max(Decimal("0"), min(Decimal("100"), Decimal("0.7") * f + Decimal("0.3") * n_score - risk))
        if price["status"] == "VERIFY_PENDING":
            decision = "VERIFY_PENDING"
            pending.extend(price["reasons"])
        elif f >= 75 and total >= 75 and price["status"] == "READY":
            decision = "BUY_RECOMMENDATION"
        else:
            decision = "WATCH"
            if f < 75:
                reasons.append("FINANCIAL_SCORE_BELOW_THRESHOLD")
            if total < 75:
                reasons.append("TOTAL_SCORE_BELOW_THRESHOLD")
            reasons.extend(price.get("reasons", []))

    return {
        "symbol": financial.symbol,
        "name": financial.name,
        "action_decision": decision,
        "label": LABELS[decision],
        "financial_score": financial.financial_score,
        "news_score": n_score,
        "risk_penalty": news["risk_penalty"],
        "total_score": total,
        "tie_breaker": tie_breaker,
        "liquidity_tier": liquidity_tier,
        "in_cooldown": in_cooldown,
        "financial_breakdown": financial.score_breakdown,
        "news_events": news["events"],
        "price": price,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "pending_checks": list(dict.fromkeys(pending)),
        "advisories": list(financial.advisories),
        "financial_evaluation_id": financial.evaluation_id,
        "recent_news_complete": news.get("recent_coverage_complete", False),
        "risk_history_complete": news.get("risk_coverage_complete", False),
    }


def _candidate_evidence_context(store, symbol: str, *, as_of: datetime, cursor, risk_urls: set[str]) -> dict:
    """Attach bounded recent news headlines, risk headlines, and DART filings for committee review."""
    news_items: list[dict] = []
    seen_news: set[str] = set()
    for kind in ("recent_archive_article", "risk_archive_article", "archive_article", "material_archive_article"):
        for record in store.records(kind, symbol, as_of=as_of):
            ev = record["payload"].get("evidence") or {}
            url = ev.get("url")
            if not url or url in seen_news:
                continue
            seen_news.add(url)
            news_items.append({
                "title": ev.get("title", ""),
                "url": url,
                "published_at": ev.get("published_at"),
                "description": (record["payload"].get("description") or "")[:200],
                "is_risk_signal": url in risk_urls,
            })
    news_items.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    risk_headlines = [item for item in news_items if item["is_risk_signal"]][:10]
    recent_headlines = [item for item in news_items if not item["is_risk_signal"]][:15]

    filings = stored_filings(store, symbol, as_of=as_of, cursor=cursor)
    filings.sort(key=lambda e: e.published_at, reverse=True)
    recent_dart = [
        {
            "title": e.title,
            "url": e.url,
            "published_at": e.published_at.isoformat(),
            "is_risk_signal": e.url in risk_urls,
        }
        for e in filings[:10]
    ]
    return {
        "recent_headlines": recent_headlines,
        "risk_headlines": risk_headlines,
        "recent_dart_filings": recent_dart,
    }


def build_recommendations(repository, store, *, as_of, expected_session=None, verified_close_at=None,
                          max_candidates=5, price_refresh_ready=True, actionable=False,
                          fallback_price_session=None, actionable_focus=None):
    as_of = validate_analysis_time(as_of)
    if type(max_candidates) is not int or not 1 <= max_candidates <= 5:
        raise ValueError("recommendation limit must be between 1 and 5")
    if not isinstance(price_refresh_ready, bool):
        raise ValueError('price_refresh_ready must be boolean')
    if not isinstance(actionable, bool):
        raise ValueError('actionable must be boolean')

    effective_session = expected_session
    effective_close = verified_close_at
    used_fallback_session = None
    if actionable and not price_refresh_ready and fallback_price_session is not None:
        effective_session = fallback_price_session
        effective_close = None
        used_fallback_session = fallback_price_session

    focus_meta: dict[str, dict] = {}
    focus_order: list[str] = []
    if actionable:
        if actionable_focus is None and effective_session is not None:
            from .research_focus import choose_actionable_focus
            actionable_focus = choose_actionable_focus(
                repository, store, as_of=as_of,
                expected_session=effective_session, verified_close_at=effective_close,
                limit=max_candidates,
            )
        if actionable_focus:
            for item in actionable_focus.get("selected", []):
                focus_meta[item["symbol"]] = item
                focus_order.append(item["symbol"])

    rows = []
    symbol_context: dict[str, tuple[object, set[str]]] = {}
    for security, financial, bundle in financial_universe(repository, store, as_of=as_of):
        cursor = store.latest("dart_cursor", security.symbol, as_of=as_of)
        attempt = store.latest("dart_attempt", security.symbol, as_of=as_of)
        current = bool(bundle and bundle["payload"].get("status") == "COLLECTED" and cursor
                       and date.fromisoformat(cursor["payload"]["end"]) >= (as_of.astimezone(KST).date() - timedelta(days=1)))
        dart_fresh = bool(bundle and bundle["payload"].get("status") == "COLLECTED" and cursor
                          and date.fromisoformat(cursor["payload"]["end"]) >= (as_of.astimezone(KST).date() - timedelta(days=7)))
        if attempt and attempt["payload"].get("status") == "FAILED" and cursor and (
                datetime.fromisoformat(attempt["observed_at"]) >= datetime.fromisoformat(cursor["observed_at"])):
            current = False
            dart_fresh = False
        inputs = [r["input_id"] for r in (bundle, cursor, attempt) if r]
        bars = []
        news_snapshot = None
        if financial.gate == "PASS":
            event_records = store.records("reviewed_event", security.symbol, as_of=as_of)
            events = [event_from_dict(r["payload"]) for r in event_records]
            inputs.extend(r["input_id"] for r in event_records)
            coverage = store.latest('recent_news_coverage', security.symbol, as_of=as_of)
            legacy = store.latest('news_coverage', security.symbol, as_of=as_of)
            if coverage is None and legacy and legacy['payload'].get('status') == 'REVIEWED':
                coverage = legacy
            if coverage:
                inputs.append(coverage["input_id"])
            if actionable:
                news_snapshot = store.latest("recent_news_snapshot", security.symbol, as_of=as_of)
                if news_snapshot:
                    inputs.append(news_snapshot["input_id"])
            risk_coverage = risk_coverage_payload(store, security.symbol, as_of=as_of)
            manual_risk = store.latest('risk_coverage', security.symbol, as_of=as_of)
            if risk_coverage is None and manual_risk and manual_risk['payload'].get('status') == 'REVIEWED':
                risk_coverage = manual_risk['payload']
                inputs.append(manual_risk['input_id'])
            if risk_coverage is None and legacy and legacy['payload'].get('status') == 'REVIEWED':
                risk_coverage = {**legacy['payload'], 'sources': ['RISK_SEARCH', 'DART_HISTORY']}
                inputs.append(legacy['input_id'])
            if risk_coverage:
                inputs.extend(risk_coverage.get('source_refs', []))
            raw = stored_filings(store, security.symbol, as_of=as_of, cursor=cursor)
            for r in store.records("news_batch", security.symbol, as_of=as_of):
                inputs.append(r["input_id"])
                raw.extend(_evidence_from_dict(e) for e in r["payload"]["evidence"])
            for r in store.records("news_archive", security.symbol, as_of=as_of):
                inputs.append(r["input_id"])
                raw.extend(_evidence_from_dict(e) for e in r["payload"]["evidence"])
            for r in store.records("archive_article", security.symbol, as_of=as_of):
                inputs.append(r["input_id"])
                raw.append(_evidence_from_dict(r["payload"]["evidence"]))
            for r in store.records('material_archive_article',security.symbol,as_of=as_of):
                inputs.append(r['input_id']);raw.append(_evidence_from_dict(r['payload']['evidence']))
            for kind in ('recent_archive_article', 'risk_archive_article'):
                for r in store.records(kind, security.symbol, as_of=as_of):
                    inputs.append(r['input_id']);raw.append(_evidence_from_dict(r['payload']['evidence']))
            news = score_news(security.symbol, events, coverage["payload"] if coverage else None, as_of=as_of,
                              raw_evidence=raw, risk_coverage=risk_coverage)
            inputs.extend(r["input_id"] for r in store.records("fcf_review", security.symbol, as_of=as_of))
            bars = repository.bars_for(security.symbol, as_of=as_of)
            price = price_review(security.symbol, bars, as_of=as_of, expected_session=effective_session, verified_close_at=effective_close)
            if not price_refresh_ready and used_fallback_session is None:
                price = {'status':'VERIFY_PENDING','reasons':['PRICE_REFRESH_UNVERIFIED']}
            if actionable:
                symbol_context[security.symbol] = (cursor, {s["url"] for s in news.get("review_signals", [])})
        else:
            news = {"news_score": None, "risk_penalty": Decimal("0"), "pending_checks": (), "hard_exclusions": (), "events": [], "review_signals": []}
            price = {"status": "NOT_EVALUATED", "reasons": []}
        if actionable:
            meta = focus_meta.get(security.symbol, {})
            row = evaluate_actionable(
                financial, news, price, as_of=as_of, dart_fresh=dart_fresh,
                news_snapshot=news_snapshot, price_fallback_session=used_fallback_session,
                tie_breaker=meta.get("tie_breaker"), liquidity_tier=meta.get("liquidity_tier"),
                in_cooldown=meta.get("in_cooldown", False),
            )
        else:
            row = evaluate_unified(financial, news, price, provenance_current=current)
        row["source_input_ids"] = sorted(set(inputs))
        row["financial_source_url"] = financial.source_inputs.get('financial', {}).get('source_url') if financial.source_inputs.get('financial') else None
        if bars:
            row["price_input_fingerprint"] = hashlib.sha256(json.dumps(to_json_value(bars), sort_keys=True).encode()).hexdigest()
        rows.append(row)
    priority = {"BUY_RECOMMENDATION": 0, "WATCH": 1, "VERIFY_PENDING": 2, "EXCLUDED": 3, "NOT_APPLICABLE": 4}
    if actionable:
        focus_rank = {sym: idx for idx, sym in enumerate(focus_order)}
        rows.sort(key=lambda r: (
            priority[r["action_decision"]],
            r["symbol"] not in focus_rank,
            focus_rank.get(r["symbol"], 999),
            -(r["total_score"] or Decimal("0")),
            -(r["financial_score"] or Decimal("0")),
            -(r.get("tie_breaker") or Decimal("0")),
            r["symbol"],
        ))
    else:
        rows.sort(key=lambda r: (priority[r["action_decision"]], -(r["total_score"] or Decimal("0")),
                                -(r["financial_score"] or Decimal("0")), r["symbol"]))
    selected = [r for r in rows if r["action_decision"] in {"BUY_RECOMMENDATION", "WATCH"}][:max_candidates]
    for i, row in enumerate(selected, 1):
        row["rank"] = i
        if actionable and row["symbol"] in symbol_context:
            cursor, risk_urls = symbol_context[row["symbol"]]
            row.update(_candidate_evidence_context(store, row["symbol"], as_of=as_of, cursor=cursor, risk_urls=risk_urls))
            meta = focus_meta.get(row["symbol"], {})
            if meta.get("chart_signal"):
                row["chart_signal"] = meta["chart_signal"]
            else:
                sel_bars = repository.bars_for(row["symbol"], as_of=as_of)
                if len(sel_bars) >= 61:
                    try:
                        from .chart_analysis import compute_full_chart_analysis
                        ca = compute_full_chart_analysis(sel_bars)
                        tp = ca["trade_plan"]
                        primary = (ca.get("patterns") or {}).get("primary")
                        row["chart_signal"] = {
                            "grade": tp["grade"],
                            "grade_label": tp["grade_label"],
                            "reasons": tp["reasons"],
                            "annotations": tp["annotations"],
                            "breakout_price": tp["breakout_price"],
                            "support_price": tp["support_price"],
                            "stop_loss": tp["stop_loss"],
                            "primary_pattern": (
                                {"code": primary["code"], "name_ko": primary["name_ko"], "status": primary["status"]}
                                if primary else None
                            ),
                        }
                    except ValueError:
                        pass
    financial_targets = [r for r in rows if r.get("financial_score") is not None and r["financial_score"] >= 70
                         and r["action_decision"] not in {"EXCLUDED", "NOT_APPLICABLE"}]
    pending_targets = sorted((r for r in financial_targets if r["action_decision"] == "VERIFY_PENDING"),
                             key=lambda r: (-r["financial_score"], r["symbol"]))[:max_candidates]
    calendar = store.latest("market_calendar", "KR", as_of=as_of)
    policy_version = POLICY_VERSION_ACTIONABLE if actionable else POLICY_VERSION
    thresholds = {"financial": 75 if actionable else 70, "total": 75}
    report = to_json_value({"contract": "UNIFIED_RESEARCH_V1", "policy_version": policy_version,
                          "policy_status": "OPERATING_APPROVED" if os.getenv('STOCK_UNIFIED_ENABLED') == 'true' else "LOCAL_VALIDATION_PROPOSAL", "as_of": as_of,
                          "score_formula": "clip(0.7*F + 0.3*N - R, 0, 100)", "thresholds": thresholds,
                          "source_read_only": True, "orders_enabled": False, "paper_account_eligible": False,
                          "recommendation_scope": "NEXT_SESSION_CONDITIONAL", "candidates": selected,
                          'research_windows': {k: v.isoformat() for k, v in news_windows(as_of).items()},
                          'coverage_scope': 'ACTIONABLE_PRICE_FIRST_WITH_COMMITTEE' if actionable else 'RECENT_NEWS_90D_AND_TARGETED_RISK_3Y_WITH_DART_10Y',
                          "research_queue": pending_targets,
                          "research_progress": {"financial_qualified": len(financial_targets),
                                                "news_scored": sum(r["news_score"] is not None for r in financial_targets),
                                                'risk_history_completed': sum(r['risk_history_complete'] for r in financial_targets),
                                                "pending_reason_counts": dict(Counter(reason.split(":", 1)[0]
                                                    for r in financial_targets for reason in r["pending_checks"]))},
                          "expected_price_session": effective_session,
                          "market_calendar_input_id": calendar["input_id"] if calendar else None, "evaluations": rows})
    if actionable:
        report["policy_mode"] = "ACTIONABLE_COMMITTEE"
        if used_fallback_session is not None:
            report["price_session_basis"] = "LATEST_AVAILABLE_SESSION"
    report['decision_counts'] = dict(Counter(row['action_decision'] for row in rows))
    report['pending_reason_counts'] = dict(Counter(reason.split(':', 1)[0] for row in rows if row['action_decision']=='VERIFY_PENDING'
                                                  for reason in row['pending_checks']))
    report["report_id"] = "UR-" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()[:24]
    return report


def _format_kst(as_of_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(as_of_iso).astimezone(KST)
        return dt.strftime("%Y-%m-%d %H:%M KST")
    except (ValueError, TypeError):
        return str(as_of_iso)


def _render_actionable_report(report: dict) -> str:
    candidates = report.get("candidates", [])
    kst_str = _format_kst(report.get("as_of", ""))
    prices = (report.get("refresh_stages") or {}).get("prices") or {}
    archive = (report.get("refresh_stages") or {}).get("archive") or {}

    session_str = report.get("expected_price_session") or prices.get("required_session") or "확인 불가"
    if report.get("price_session_basis") == "LATEST_AVAILABLE_SESSION":
        price_summary = f"기준일봉: {session_str} (당일 일봉 미발행)"
    else:
        price_summary = f"기준일봉: {session_str}"

    archive_status = archive.get("status")
    archive_error = archive.get("error_code")
    if archive_status == "UNAVAILABLE":
        news_summary = "뉴스: 쿨다운(중립 기준)" if archive_error == "NEWS_REFRESH_COOLDOWN" else "뉴스: 미수집(중립 기준)"
    elif archive.get("actual_calls"):
        news_summary = f"뉴스: 최근 기사 수집 완료({archive['actual_calls']}회)"
    else:
        news_summary = "뉴스: 최근 스냅샷 반영"

    lines = [
        f"📊 장기투자 종목 리서치 ({kst_str})",
        f"보고서: {report['report_id']}",
        f"{price_summary} · {news_summary}",
        "",
        f"[오늘의 후보 {len(candidates)}종목]",
    ]
    for idx, row in enumerate(candidates, 1):
        strategy = STRATEGY_LABELS.get((row.get("price") or {}).get("strategy", ""), "")
        cs = row.get("chart_signal") or {}
        grade_tag = f" · {cs['grade_label']}" if cs.get("grade_label") else (f" · {strategy}" if strategy else "")
        pat = cs.get("primary_pattern")
        pat_tag = f" · {pat['name_ko']}" if isinstance(pat, dict) and pat.get("name_ko") else ""
        lines.append(f"{idx}. {row['name']} ({row['symbol']}) · 총점 {row['total_score']}{grade_tag}{pat_tag}")

    lines.extend(["", "━━━━━━━━━━━━━━━"])

    for idx, row in enumerate(candidates):
        badge = _RANK_BADGES[idx] if idx < len(_RANK_BADGES) else f"{idx + 1}."
        strategy = STRATEGY_LABELS.get((row.get("price") or {}).get("strategy", ""), "")
        strategy_suffix = f" · {strategy}" if strategy else ""
        decision_short = "매수 추천" if row.get("action_decision") == "BUY_RECOMMENDATION" else row.get("label", "")
        lines.append("")
        lines.append(f"{badge} {row['name']} ({row['symbol']}) · {decision_short}{strategy_suffix}")
        lines.append(f"• 점수: 총점 {row['total_score']} (재무 {row['financial_score']} / 뉴스 {row['news_score']} / 위험 -{row['risk_penalty']})")
        metrics = (row.get("price") or {}).get("metrics") or {}
        if metrics.get("average_value20") and metrics.get("return20") is not None:
            avg_eok = Decimal(str(metrics["average_value20"])) / Decimal("100000000")
            ret20 = Decimal(str(metrics["return20"])) * Decimal("100")
            dd60 = Decimal(str(metrics["drawdown60"])) * Decimal("100")
            lines.append(f"• 시세: 거래대금 {avg_eok:,.1f}억 · 20일 {ret20:+.1f}% · 60일 고점대비 {dd60:+.1f}%")
        cs = row.get("chart_signal") or {}
        if cs.get("grade_label"):
            c_parts = [cs["grade_label"]]
            pat = cs.get("primary_pattern")
            if isinstance(pat, dict) and pat.get("name_ko"):
                st_ko = "돌파" if pat.get("status") == "BREAKOUT" else "이탈" if pat.get("status") == "BREAKDOWN" else "형성"
                c_parts.append(f"{pat['name_ko']}({st_ko})")
            lvls = []
            if cs.get("breakout_price") is not None:
                lvls.append(f"돌파 {float(cs['breakout_price']):,.0f}")
            if cs.get("support_price") is not None:
                lvls.append(f"지지 {float(cs['support_price']):,.0f}")
            if cs.get("stop_loss") is not None:
                lvls.append(f"손절 {float(cs['stop_loss']):,.0f}")
            if lvls:
                c_parts.append(" / ".join(lvls) + "원")
            if cs.get("reasons"):
                c_parts.append(", ".join(cs["reasons"][:2]))
            lines.append("• 차트: " + " · ".join(c_parts))

        # Keep only issuer-specific warnings/advisories; common notices live once in header/footer.
        specific_notes = []
        for w in row.get("warnings", []):
            if w.startswith("⚠️ 가격 기준:") or "최근 뉴스 미수집" in w:
                continue
            specific_notes.append(w.removeprefix("⚠️ 확인 필요: ").strip())
        for adv in row.get("advisories", []):
            if adv == "LEGACY_FINANCIAL_AVAILABILITY_AND_REVISION_UNCERTIFIED":
                continue
            specific_notes.append(_ADVISORY_LABELS.get(adv, adv))
        if specific_notes:
            lines.append("• ⚠️ 확인: " + " · ".join(dict.fromkeys(specific_notes)))
        if row.get("financial_source_url"):
            lines.append(f"• 원문: {row['financial_source_url']}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━",
        "📌 공통 진입·무효화 조건",
        "• 다음 거래일 최신 시세로 유동성·가격 조건 재확인",
        "• 종가 20일선 이탈 또는 새 악재·정정·취소 발생 시 추천 무효",
        "• Naver 색인 범위 참고 · 자동 주문 및 모의계좌 연동 없음",
    ])
    return "\n".join(lines)


def render_recommendations(report):
    if report.get("policy_mode") == "ACTIONABLE_COMMITTEE" and report.get("candidates"):
        return _render_actionable_report(report)
    fin_threshold = (report.get("thresholds") or {}).get("financial", 70)
    tot_threshold = (report.get("thresholds") or {}).get("total", 75)
    lines = ["통합 종목 연구 · 다음 거래일 조건부", f"보고서: {report['report_id']}", f"기준: {report['as_of']}", f"총점 = 재무 70% + 뉴스 30% - 위험 감점 · 기준 F≥{fin_threshold} / 총점≥{tot_threshold}"]
    prices=report.get('refresh_stages',{}).get('prices')
    if prices:
        if report.get('price_session_basis') == 'LATEST_AVAILABLE_SESSION' and report.get('expected_price_session'):
            lines.append(f"가격 자료: 직전 확보 거래일 기준 ({report['expected_price_session']}) · 당일 일봉 {prices.get('required_session') or prices.get('target_date') or '미발행'}")
        else:
            lines.append('가격 자료: '+('확보 완료' if prices.get('status')=='READY' else '확인 대기')+' · 필요한 일봉 '+str(prices.get('required_session') or prices.get('target_date') or '확인 불가'))
    reasons={'COMPANY_KIND_UNAVAILABLE':'기업 유형 미확인','DART_COLLECTION_NOT_CURRENT':'공시 갱신 대기',
             'NEWS_HISTORY_COVERAGE_UNAVAILABLE':'3년 뉴스 검토 미완료','MATERIAL_EVENT_REVIEW_REQUIRED':'위험 사건 원문 검토 항목',
             'NEWS_RECENT_COVERAGE_UNAVAILABLE':'최근 90일 뉴스 조사 미완료',
             'RISK_HISTORY_COVERAGE_UNAVAILABLE':'과거 3년 위험 조사 미완료',
             'UNRESOLVED_NEGATIVE_EVENT':'미해소 위험 사건',
             'PRICE_REFRESH_UNVERIFIED':'최신 가격 확인 대기','PRICE_SESSION_INCOMPLETE':'종목 일봉 부족',
             'THREE_YEAR_PROFIT_HISTORY_UNAVAILABLE':'3개년 이익 이력 부족','TURNOVER_HISTORY_UNAVAILABLE':'회전율 이력 부족',
             'FCF_CAUSE_REVIEW_REQUIRED':'음수 FCF 원인 검토'}
    archive = report.get('refresh_stages', {}).get('archive', {})
    if report.get('research_windows'):
        windows = report['research_windows']
        lines.append('뉴스 평가: 최근 90일 ' + windows['recent_start'] + '~' + windows['end']
                     + ' · 위험 조사: 과거 3년 ' + windows['risk_start'] + '~' + windows['end'])
    archive_rows = archive.get('results', [archive] if archive.get('symbol') else [])
    status_labels = {'PARTIAL': '조사 중', 'COMPLETE': '검색 완료', 'PROVIDER_LIMIT': '검색 한도 도달', 'SNAPSHOT_100': '최신 100건 수집'}
    if archive_rows:
        lines.append('이번 뉴스 수집: ' + ', '.join(r['symbol'] + ' ' + status_labels.get(r['status'], r['status'])
                                                  for r in archive_rows)
                     + ' · 요청 ' + str(archive.get('actual_calls', 0)) + '회')
    material = report.get('refresh_stages', {}).get('material_archive', {})
    if material.get('symbol'):
        lines.append('위험 검색 보완: ' + material['symbol'] + ' · '
                     + status_labels.get(material['status'], material['status'])
                     + ' · 요청 ' + str(material.get('actual_calls', 0)) + '회')
    actionable_with_candidates = report.get("policy_mode") == "ACTIONABLE_COMMITTEE" and bool(report.get("candidates"))
    if not report["candidates"]:
        lines.append("추천·관찰 기준을 충족한 종목 없음")
        rows=report.get('evaluations',[])
        qualified=[r for r in rows if r.get('financial_score') is not None and Decimal(str(r['financial_score']))>=70
                   and r['action_decision'] not in {'EXCLUDED','NOT_APPLICABLE'}]
        lines.append('재무 70점 이상 검토 대상 '+str(len(qualified))+'개 · 뉴스 점수 확보 '+str(sum(r.get('news_score') is not None for r in qualified))+'개')
        lines.append('판정 집계: ' + ', '.join(LABELS.get(k,k) + ' ' + str(v) for k,v in report.get('decision_counts', {}).items()))
        lines.append('미완료: ' + ', '.join(reasons.get(k,k) + ' ' + str(v) for k,v in sorted(report.get('pending_reason_counts', {}).items(), key=lambda i:-i[1])[:5]))
    progress = report.get('research_progress')
    if not actionable_with_candidates and progress and progress['pending_reason_counts']:
        lines.append('재무 70점 이상 대상의 미완료: ' + ', '.join(
            reasons.get(k, k) + ' ' + str(v) + ('항목' if k == 'MATERIAL_EVENT_REVIEW_REQUIRED' else '개')
            for k, v in sorted(progress['pending_reason_counts'].items(), key=lambda item: -item[1])[:5]))
    queue = report.get('research_queue', [])
    if not actionable_with_candidates and queue:
        lines.append('재무 점수 상위 검증 대기 종목')
        for row in queue:
            pending_counts = Counter(reason.split(':', 1)[0] for reason in row['pending_checks'])
            missing = ', '.join(reasons.get(k, k) + (' ' + str(v) + '항목' if k == 'MATERIAL_EVENT_REVIEW_REQUIRED' else '')
                                for k, v in pending_counts.items())
            news_score = row['news_score'] if row['news_score'] is not None else '확인 불가'
            total_score = row['total_score'] if row['total_score'] is not None else '확인 불가'
            lines.append(f"{row['name']} ({row['symbol']}) · 재무 {row['financial_score']} / 뉴스 {news_score} / 총점 {total_score} · 확인 대기: {missing}")
    for row in report["candidates"]:
        strategy = STRATEGY_LABELS.get((row.get("price") or {}).get("strategy", ""), "")
        strategy_suffix = f" · {strategy}" if strategy else ""
        lines.extend([
            f"{row['rank']}. {row['name']} ({row['symbol']}) · {row['label']}{strategy_suffix}",
            f"재무 {row['financial_score']} / 뉴스 {row['news_score']} / 위험 -{row['risk_penalty']} / 총점 {row['total_score']}",
        ])
        metrics = (row.get("price") or {}).get("metrics") or {}
        if metrics.get("average_value20") and metrics.get("return20") is not None:
            avg_eok = Decimal(str(metrics["average_value20"])) / Decimal("100000000")
            ret20 = Decimal(str(metrics["return20"])) * Decimal("100")
            dd60 = Decimal(str(metrics["drawdown60"])) * Decimal("100")
            lines.append(f"일봉({row['price'].get('session', '')}): 20일 거래대금 {avg_eok:,.1f}억 · 20일 수익률 {ret20:+.1f}% · 60일 고점대비 {dd60:+.1f}%")
        for warning in row.get("warnings", []):
            lines.append(warning)
        if row["advisories"]:
            lines.append("참고: " + ", ".join(row["advisories"]))
        lines.extend(row["price"].get("entry_conditions", []))
        if row.get('financial_source_url'):
            lines.append('재무 원문: ' + row['financial_source_url'])
    lines.append("최근 뉴스와 지정 위험 검색은 Naver 색인 범위 · 전체 과거 기사 검토를 뜻하지 않음 · 한도/미해소 악재는 확인 대기 · 자동 주문 및 기존 모의계좌 연동 없음")
    return "\n".join(lines)
