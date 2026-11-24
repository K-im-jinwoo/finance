"""Bound paid research to ten issuers while retaining completed work and rotation."""
from datetime import datetime, timedelta
from decimal import Decimal

from .unified_research import financial_universe, price_review

LARGE_LIQUIDITY_KRW = Decimal("50000000000")
MID_LIQUIDITY_KRW = Decimal("5000000000")
LIQUIDITY_TIERS = ("LARGE", "MID", "SMALL")


def choose_focus(repository, store, *, as_of):
    eligible = {s.symbol: f for s, f, _ in financial_universe(repository, store, as_of=as_of)
                if f.gate == 'PASS' and f.financial_score >= 70}
    rounds = store.latest_by_symbol('search_round_completed', as_of=as_of)
    latest = store.latest('unified_report', 'ALL', as_of=as_of)
    monitored = [r['symbol'] for r in (latest or {}).get('payload', {}).get('candidates', [])
                 if r['symbol'] in eligible and r['action_decision'] in {'BUY_RECOMMENDATION', 'WATCH'}][:5]
    remaining = [symbol for symbol in eligible if symbol not in monitored]
    remaining.sort(key=lambda symbol: (symbol in rounds, (rounds.get(symbol) or {}).get('observed_at', ''),
                                       -eligible[symbol].financial_score, symbol))
    return monitored + remaining[:10-len(monitored)]


def _liquidity_tier(avg_value20: Decimal) -> str:
    if avg_value20 >= LARGE_LIQUIDITY_KRW:
        return "LARGE"
    if avg_value20 >= MID_LIQUIDITY_KRW:
        return "MID"
    return "SMALL"


def _tie_breaker_score(financial, price: dict) -> Decimal:
    """Deterministic 0.00..0.99 tie-breaker from verified cash quality and price setup."""
    snap = financial.source_inputs.get("financial") or {}
    op = Decimal(str(snap.get("operating_income") or "0"))
    ocf = Decimal(str(snap.get("operating_cash_flow") or "0"))
    cash_quality = min(Decimal("2"), max(Decimal("0"), ocf / op)) / Decimal("2") if op > 0 else Decimal("0")

    metrics = price.get("metrics") or {}
    ret20 = Decimal(str(metrics.get("return20") or "0"))
    dd60 = Decimal(str(metrics.get("drawdown60") or "0"))
    vol_ratio = Decimal(str(metrics.get("volume_ratio20") or "0"))
    if price.get("strategy") == "BOTTOM_REBOUND":
        setup_strength = min(Decimal("1"), max(Decimal("0"), -dd60 / Decimal("0.50")))
    else:
        setup_strength = min(Decimal("1"), max(Decimal("0"), ret20 / Decimal("0.40")))
    vol_strength = min(Decimal("1"), max(Decimal("0"), vol_ratio / Decimal("3")))

    raw = Decimal("0.40") * cash_quality + Decimal("0.40") * setup_strength + Decimal("0.20") * vol_strength
    return min(Decimal("0.99"), max(Decimal("0.00"), raw.quantize(Decimal("0.01"))))


def recent_recommended_symbols(store, *, as_of, cooldown_days: int = 30) -> set[str]:
    """Collect symbols recently emitted as BUY_RECOMMENDATION or WATCH within cooldown_days."""
    cutoff = as_of - timedelta(days=cooldown_days)
    recent: set[str] = set()
    if hasattr(store, "recent_unified_candidates"):
        candidate_lists = store.recent_unified_candidates(as_of=as_of, since=cutoff)
    else:
        candidate_lists = [
            record["payload"].get("candidates", [])
            for record in store.records("unified_report", "ALL", as_of=as_of)
            if datetime.fromisoformat(record["observed_at"]) >= cutoff
        ]
    for candidates in candidate_lists:
        for row in candidates:
            if isinstance(row, dict) and row.get("action_decision") in {"BUY_RECOMMENDATION", "WATCH"} and row.get("symbol"):
                recent.add(row["symbol"])
    return recent


def compare_final_candidates(store, candidates, *, as_of, policy_version):
    """Describe changes from the previous saved report without influencing selection."""
    # The store compares Julian dates at millisecond precision. Exclude the current report on rebuilds.
    previous = store.latest("unified_report", "ALL", as_of=as_of - timedelta(milliseconds=1))
    if previous is None or previous["payload"].get("policy_version") != policy_version:
        return None
    prior = {row["symbol"]: row for row in previous["payload"].get("candidates", [])}
    changes = {}
    for row in candidates:
        symbol = row["symbol"]
        old = prior.get(symbol)
        if old is None:
            changes[symbol] = {"new": True}
            continue
        change = {}
        if old.get("total_score") is not None and Decimal(str(old["total_score"])) != Decimal(str(row["total_score"])):
            change["previous_total_score"] = old["total_score"]
        elif any(old.get(key) is not None and Decimal(str(old[key])) != Decimal(str(row[key]))
                 for key in ("financial_score", "news_score", "risk_penalty")):
            change["component_scores_changed"] = True
        if old.get("action_decision") and old["action_decision"] != row["action_decision"]:
            change["previous_decision"] = old["action_decision"]
        if change:
            changes[symbol] = change
    return {"previous_as_of": previous["payload"].get("as_of") or previous["observed_at"], "changes": changes}


def _select_diverse(ranked: list[dict], limit: int) -> list[dict]:
    """Select up to limit candidates across LARGE/MID/SMALL liquidity tiers, respecting cooldown."""
    selected: list[dict] = []
    seen: set[str] = set()
    for cooldown_flag in (False, True):
        group = [item for item in ranked if item["in_cooldown"] is cooldown_flag]
        buckets = {tier: [item for item in group if item["liquidity_tier"] == tier] for tier in LIQUIDITY_TIERS}
        while len(selected) < limit and any(buckets.values()):
            progressed = False
            for tier in LIQUIDITY_TIERS:
                if len(selected) >= limit:
                    break
                if buckets[tier]:
                    candidate = buckets[tier].pop(0)
                    if candidate["symbol"] not in seen:
                        selected.append(candidate)
                        seen.add(candidate["symbol"])
                        progressed = True
            if not progressed:
                break
    return selected


def choose_actionable_focus(
    repository,
    store,
    *,
    as_of,
    expected_session,
    verified_close_at=None,
    min_financial_score: int = 75,
    limit: int = 5,
    cooldown_days: int = 30,
) -> dict:
    """Filter F>=min_financial_score AND price==READY first, then rank with tie-breaker & diversity."""
    if expected_session is None:
        return {"ready_pool": [], "selected": [], "cooldown_symbols": []}
    cooldown = recent_recommended_symbols(store, as_of=as_of, cooldown_days=cooldown_days)
    ready: list[dict] = []
    for security, financial, _ in financial_universe(repository, store, as_of=as_of):
        if financial.gate != "PASS" or financial.financial_score is None:
            continue
        if financial.financial_score < Decimal(min_financial_score):
            continue
        bars = repository.bars_for(security.symbol, as_of=as_of)
        price = price_review(
            security.symbol, bars, as_of=as_of,
            expected_session=expected_session, verified_close_at=verified_close_at,
        )
        if price["status"] != "READY":
            continue
        avg_val = Decimal(str(price["metrics"]["average_value20"]))
        tie = _tie_breaker_score(financial, price)
        chart_signal = None
        if len(bars) >= 61:
            try:
                from .chart_analysis import compute_full_chart_analysis
                ca = compute_full_chart_analysis(bars)
                tp = ca["trade_plan"]
                bonus = Decimal(str(tp.get("scoring_bonus") or 0))
                tie = max(Decimal("0.00"), (tie + bonus).quantize(Decimal("0.01")))
                primary = (ca.get("patterns") or {}).get("primary")
                chart_signal = {
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
        ready.append({
            "symbol": security.symbol,
            "name": security.name,
            "financial_score": financial.financial_score,
            "tie_breaker": tie,
            "liquidity_tier": _liquidity_tier(avg_val),
            "average_value20": avg_val,
            "in_cooldown": security.symbol in cooldown,
            "price": price,
            "chart_signal": chart_signal,
        })
    ready.sort(key=lambda item: (
        item["in_cooldown"],
        -item["financial_score"],
        -item["tie_breaker"],
        -item["average_value20"],
        item["symbol"],
    ))
    selected = _select_diverse(ready, limit)
    return {
        "ready_pool": [item["symbol"] for item in ready],
        "selected": selected,
        "cooldown_symbols": sorted(cooldown),
    }
