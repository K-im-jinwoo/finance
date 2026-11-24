"""Reports derived from audited simulated fills and their market data source."""
from __future__ import annotations

from decimal import Decimal
import json

from .contracts import KST, canonical, instant, symbol
from .risk import RiskState

PROVISIONAL_COST_STATUS = "PROVISIONAL_DEV_UNAPPROVED"
APPROVED_COST_STATUS = "APPROVED_CONSERVATIVE_V1"


def _symbol_names(ledger):
    row = ledger.connection.execute("SELECT value FROM meta WHERE key='symbol_names'").fetchone()
    names = json.loads(row[0]) if row else {}
    if not isinstance(names, dict):
        return {}
    valid = {}
    for code, name in names.items():
        try:
            symbol(code)
        except ValueError:
            continue
        if isinstance(name, str) and 0 < len(name.strip()) <= 200 and not any(c in name for c in "\r\n\0"):
            valid[code] = name.strip()
    return valid


def _name_overlay(report, names):
    def named(row):
        code = row.get("symbol")
        return {**row, "name":names[code]} if code in names else dict(row)
    report["symbol_names"] = names
    report["scan_history"] = [{**scan, "selected":[named(row) for row in scan["selected"]],
        "rejected":[named(row) for row in scan["rejected"]]} for scan in report["scan_history"]]
    for key in ("fills", "signals", "decisions", "quality_events"):
        report[key] = [named(row) for row in report[key]]


def _cost_policy(costs, meta):
    status = PROVISIONAL_COST_STATUS if meta.get("cost_policy_status") == PROVISIONAL_COST_STATUS else "FIXTURE_ASSUMPTION"
    if meta.get("cost_policy_status") == APPROVED_COST_STATUS:
        # Check the immutable cost vector too: metadata alone cannot promote a
        # custom/provisional ledger. Lazy import avoids the stream/report cycle.
        from dataclasses import asdict
        from .stream import approved_costs
        if costs == asdict(approved_costs()):
            status = APPROVED_COST_STATUS
    if costs is None:
        return status, "비용 기준 미설정 · 공식 요율 확인 불가"
    prefix = f"매수 {costs['buy_fee_bps']}bps · 매도 {costs['sell_fee_bps']}bps · 매도세 {costs['sell_tax_bps']}bps · "
    if status == APPROVED_COST_STATUS:
        return status, prefix + ("승인된 보수적 표준안(산정 가정: KRX 1.5bps + 유관기관 제비용 ~0.4bps 여유 반영 · "
                                "2026 일반주식 거래세 20bps) · 실주문 없음 · 실제 계좌 청구율과 다를 수 있습니다.")
    label = "개발용 잠정 보수안" if status == PROVISIONAL_COST_STATUS else "합성 모의거래 비용 가정"
    return status, prefix + label + " · 공식 요율 확정 전이며 실제 적용 요율이 아닙니다."


def trading_inventory(fills):
    """Weighted acquisition cost includes buy fees; each sell realizes its costs."""
    inventory, completed, details, cycles = {}, [], [], {}
    for fill in fills:
        code, qty = fill["symbol"], fill["quantity"]
        gross, fee, tax = (Decimal(fill[key]) for key in ("gross", "fee", "tax"))
        holding = inventory.setdefault(code, {"quantity": 0, "cost": Decimal(0)})
        cycle = cycles.setdefault(code, {"symbol": code, "entry_at": fill["at"],
            "buy_quantity": 0, "buy_gross": Decimal(0), "sell_gross": Decimal(0),
            "fees": Decimal(0), "tax": Decimal(0), "realized_pnl": Decimal(0)})
        pnl = None
        if fill["side"] == "BUY":
            holding["quantity"] += qty
            holding["cost"] += gross + fee
            cycle["buy_quantity"] += qty
            cycle["buy_gross"] += gross
        else:
            if qty > holding["quantity"]:
                raise ValueError("sell quantity exceeds independently reconstructed inventory")
            basis = holding["cost"] if qty == holding["quantity"] else holding["cost"] * qty / holding["quantity"]
            pnl = gross - fee - tax - basis
            holding["quantity"] -= qty
            holding["cost"] -= basis
            cycle["sell_gross"] += gross
            cycle["realized_pnl"] += pnl
        cycle["fees"] += fee
        cycle["tax"] += tax
        details.append({**fill, "price": gross / qty, "realized_pnl": pnl})
        if not holding["quantity"]:
            completed.append({**cycle, "exit_at": fill["at"],
                "average_buy_price": cycle["buy_gross"] / cycle["buy_quantity"],
                "average_sell_price": cycle["sell_gross"] / cycle["buy_quantity"]})
            del cycles[code]
    return inventory, completed, details


def valuation_point_counts(curve):
    total = missing = 0
    complete = True
    for sample in curve:
        if sample.get("diagnostic_truncation"):
            total += sample.get("omitted_valuation_points", 0)
            missing += sample.get("omitted_missing_valuation_points", 0)
            complete &= sample.get("valuation_point_counts_complete", "omitted_valuation_points" in sample)
        else:
            total += 1
            missing += sample["equity"] is None
    return total, missing, complete


def _drawdown(curve, capital):
    total, missing, complete = valuation_point_counts(curve)
    peak, worst, uncertain = capital, Decimal(0), not bool(curve)
    observed = False
    for sample in curve:
        if sample["equity"] is None:
            uncertain = True
            continue
        equity = Decimal(sample["equity"])
        observed = True
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) * 100 / peak)
    return {"mdd_pct": None if uncertain else worst, "mdd_observed_pct": worst if observed else None,
            "mdd_basis": "RECORDED_VALUATIONS_ONLY", "mdd_uncertain": uncertain,
            "valuation_points": total, "missing_valuation_points": missing,
            "valuation_missing_pct": Decimal(missing * 100) / total if total else None,
            "valuation_point_counts_complete": complete}


def build_trading_report(ledger, *, scan_history=(), signals=(), simulation_meta=None):
    ledger.audit()
    snapshot = ledger.snapshot()
    events = [json.loads(row[5]) for row in snapshot["events"]]
    persisted = next((e["data"]["_bot_summary"] for e in reversed(events)
                      if "_bot_summary" in e["data"]), {})
    latest_row = ledger.connection.execute("SELECT value FROM meta WHERE key='_bot_summary'").fetchone()
    if latest_row:
        latest = json.loads(latest_row[0])
        event_sequence = next((row[1] for row in reversed(snapshot["events"])
            if "_bot_summary" in json.loads(row[5])["data"]), -1)
        if latest.get("_checkpoint_sequence", -1) >= event_sequence:
            persisted = latest
    meta = simulation_meta if simulation_meta is not None else persisted or ledger.meta
    costs = ledger.meta["fixture_costs"]
    provenance = meta.get("stream_provenance") or ledger.meta.get("stream_provenance")
    if provenance and provenance.get("kind") in {"RECORDED", "LIVE_RECORDED"}:
        data_source = "LIVE_RECORDED"
    else:
        data_source = "SYNTHETIC" if ledger.meta["synthetic"] else "OBSERVATION"
    execution_mode = "SIMULATED_FILL" if ledger.meta["synthetic"] and costs is not None else "OBSERVATION_ONLY"
    cost_status, cost_notice = _cost_policy(costs, meta)
    scans = list(scan_history) if scan_history else meta.get("scan_history", [])
    signals = list(signals) if signals else meta.get("signals", [])
    curve = meta.get("equity_curve", [])
    order = {row[0]: row[1] for row in snapshot["events"]}
    inventory, trades, fills = trading_inventory(sorted(snapshot["fills"], key=lambda f: order[f["event_id"]]))
    intents = {e["data"].get("intent_id", e["id"]): e for e in events if e["kind"] == "INTENT"}
    for fill in fills:
        intent = intents.get(fill["intent_id"], {})
        fill["signal"] = intent.get("data", {}).get("strategy_signal")
    at = events[-1]["at"] if events else None
    valuation = ledger.valuation(at) if at else {"status": "KNOWN", "equity": Decimal(ledger.meta["initial_capital"])}
    capital = Decimal(ledger.meta["initial_capital"])
    realized = sum((f["realized_pnl"] for f in fills if f["realized_pnl"] is not None), Decimal(0))
    net = valuation["equity"] - capital if valuation["equity"] is not None else None
    risk_events, previous = [], None
    for sample in curve:
        key = (sample["phase"], tuple(sample["locks"]))
        if key != previous:
            risk_events.append({k: sample[k] for k in ("at", "phase", "locks")})
            previous = key
    quality = [{"id": row[0], "at": row[3], "issues": json.loads(row[6])["quality_issues"],
                "symbol":json.loads(row[5])["data"].get("symbol")}
               for row in snapshot["events"] if json.loads(row[6]).get("quality_issues")]
    report = {"mode": ledger.meta["mode"], "profile": meta.get("profile", "UNSPECIFIED"),
        "dataset_kind": data_source, "data_source": data_source, "execution_mode": execution_mode,
        "orders_enabled": False, "paper_ready": bool(meta.get("paper_ready", False)) and cost_status == APPROVED_COST_STATUS,
        "scope": "INTEGRATED_KR",
        "policy_version": ledger.meta["policy_version"], "seed": ledger.meta["replay_seed"],
        "fixture_costs": ledger.meta["fixture_costs"], "order_strength": "UNAVAILABLE", "tick_vwap": "UNAVAILABLE",
        "cost_policy_status": cost_status, "cost_policy_notice": cost_notice,
        "stream_provenance": {**provenance, "kind": data_source} if provenance else None,
        "account": {"initial_capital": capital, "cash": Decimal(snapshot["state"]["cash"]),
            "final_equity": valuation["equity"], "valuation_status": valuation["status"],
            "realized_pnl_net": realized, "net_pnl": net, "return_pct": net * 100 / capital if net is not None else None,
            "completed_trades": len(trades),
            "win_rate_pct": Decimal(sum(t["realized_pnl"] > 0 for t in trades)) * 100 / len(trades) if trades else None,
            **_drawdown(curve, capital)},
        "sessions": snapshot["state"]["sessions"],
        "positions": {code: holding for code, holding in inventory.items() if holding["quantity"]},
        "scan_history": scans, "signals": signals, "intents": list(snapshot["state"]["intents"].items()),
        "fills": fills, "completed_trades": trades, "decisions": meta.get("decisions", []),
        "inactive_intents": meta.get("inactive_intents", []),
        "risk_events": risk_events, "quality_events": quality, "equity_curve": curve,
        "audit": ledger.audit(), "ledger_hash": ledger.state_hash,
        "dataset_hash": meta.get("dataset_hash")}
    risk = RiskState.restore(snapshot["state"]["risk"])
    report["current_risk"] = {"phase":str(risk.phase), "locks":sorted(risk.locks),
        "reasons":risk.reasons, "entry_allowed":bool(report["paper_ready"] and risk.candidate_entry_allowed)}
    _name_overlay(report, _symbol_names(ledger))
    # JSON numbers never silently lose decimal precision in downstream consumers.
    return json.loads(canonical(report))


def _kst(value):
    return instant(value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def _display(value):
    return "확인 불가" if value is None else str(value)


def render_markdown_report(report):
    account = report["account"]
    data_source = report.get("data_source") or report["dataset_kind"]
    title = "당일 단타 모의거래 리포트" if data_source == "LIVE_RECORDED" else "당일 단타 오프라인 모의거래 리포트"
    mdd_note = "MDD는 기록된 평가 시점 기준입니다. 평가 공백이 있으면 전체 MDD는 확인 불가로 표시합니다."
    if account.get("valuation_point_counts_complete") is False:
        mdd_note += " 평가점 누락률의 분모는 개수를 확인할 수 있는 평가점입니다."
    lines = [f"# {title}", "",
        f"모드: {report['mode']} / {report['profile']}",
        f"데이터 출처: {data_source} · 체결 방식: {_display(report.get('execution_mode'))}", "",
        f"orders_enabled=false · PAPER_READY={str(report['paper_ready']).lower()} · KRX+NXT 통합 시세(INTEGRATED_KR)", "",
        "명시한 가정 비용을 적용한 모의 결과입니다. "
        "체결강도·틱 VWAP은 미제공이며 분봉 VWAP은 근사치입니다.", "",
        f"가정 비용: `{canonical(report['fixture_costs'])}` (bps, participation 비율, latency_ms 밀리초)", "",
        f"비용 정책: {report['cost_policy_status']} · {report['cost_policy_notice']}", "",
        "## 계좌 요약", "", "| 항목 | 값 |", "|---|---:|",
        *[f"| {label} | {_display(account.get(key))} |" for label, key in (
            ("초기 자본(원)", "initial_capital"), ("최종 현금(원)", "cash"), ("최종 평가자산(원)", "final_equity"),
            ("평가 상태", "valuation_status"), ("비용 차감 실현 손익(원)", "realized_pnl_net"),
            ("총 순손익(원)", "net_pnl"), ("수익률(%)", "return_pct"), ("완결 거래 수", "completed_trades"),
            ("승률(%)", "win_rate_pct"), ("최대 낙폭 MDD(%)", "mdd_pct"),
            ("평가점 누락률(%)", "valuation_missing_pct"), ("관측 구간 낙폭(%)", "mdd_observed_pct"))], "",
        mdd_note, "",
        f"세션 종료 상태: `{canonical(report['sessions'])}`", "", "## 종목 탐색", ""]
    for scan in report["scan_history"]:
        lines.append(f"- {_kst(scan['at'])} KST")
        for row in scan["selected"]:
            lines.append(f"  - 선정 {row['symbol']} {row['name']}: 거래대금 순위 {row['rank_turnover']}, "
                f"거래량 순위 {row['rank_volume']}, 거래대금 {row['turnover']}원, 스프레드 "
                f"{_display(row['spread_bps'])}bps, 사유 {', '.join(row['selection_reasons'])}")
        for row in scan["rejected"]:
            lines.append(f"  - 제외 {row['symbol']}: {', '.join(row['reasons'])}")
    lines.extend(["", "## 체결 내역", "", "| 시각(KST) | 종목 | 매수/매도 | 수량 | 평균 체결가 | 수수료 | 세금 | 실현 순손익 | 사유 |",
                  "|---|---|---|---:|---:|---:|---:|---:|---|"])
    for fill in report["fills"]:
        signal = fill.get("signal") or {}
        reason = signal.get("exit_reason") or ", ".join(signal.get("reasons", []))
        lines.append(f"| {_kst(fill['at'])} | {fill['symbol']} | {fill['side']} | {fill['quantity']} | "
            f"{fill['price']} | {fill['fee']} | {fill['tax']} | {_display(fill['realized_pnl'])} | {reason} |")
    lines.extend(["", "## 신호 내역", ""])
    for item in report["signals"]:
        lines.append(f"- {_kst(item['at'])} {item['symbol']} {item['action']} ({item['score']}): "
                     f"{item['exit_reason'] or ', '.join(item['reasons'])}")
    lines.extend(["", "## 위험·품질 및 실행 판단", ""])
    for item in report["risk_events"]:
        lines.append(f"- {_kst(item['at'])}: {item['phase']} / {', '.join(item['locks']) or '잠금 없음'}")
    for item in report["quality_events"]:
        lines.append(f"- {_kst(item['at'])}: {', '.join(item['issues'])}")
    for item in report["decisions"]:
        lines.append(f"- {_kst(item['at'])} {item.get('symbol', '')}: {item['status']} / {item['reason']}")
    return "\n".join(lines) + "\n"
