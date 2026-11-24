"""Korean paper-fill notices and durable, injected at-most-once dispatch.

No credentials, HTTP client or implicit sending live here. A reservation is
committed before calling the sender. Failed/uncertain attempts are never retried
automatically: Telegram does not supply an idempotency key for sendMessage.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json

from ..telegram_routing import BotChannel, with_channel_header
from .contracts import KST, canonical, instant, symbol
from .report import _symbol_names, trading_inventory

DELIVERIES_KEY = "telegram_fill_deliveries_v1"
SENT_IDS_KEY = "telegram_notified_fill_ids"
DAILY_SUMMARY_KEY = "telegram_daily_summary_v1"
DELIVERY_POLICY = 'AT_MOST_ONCE'
REASONS = {"VWAP_BREAKOUT":"분봉 근사 VWAP 돌파", "RISING_CLOSE":"완료 분봉 상승",
    "VOLUME_SPIKE":"완료 분봉 거래량 급증", "BID_DEPTH":"매수호가 잔량 충족",
    "TIME_EXIT":"시간 청산", "LOSS_EXIT":"일손실 한도 청산", "TAKE_PROFIT":"익절",
    "TRAILING_STOP":"트레일링 익절", "STOP_LOSS":"손절", "MOMENTUM_WEAK":"모멘텀 약화"}


def _amount(value):
    if isinstance(value, (bool, float)):
        raise ValueError("exact decimal amount required")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("finite amount required")
    return result


def _won(value, *, signed=False):
    amount = _amount(value)
    rounded = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    prefix = "+" if signed and rounded > 0 else ""
    return prefix + format(rounded, ",.0f") + "원"


def _korean_won(value):
    rounded = int(_amount(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    remaining, parts = abs(rounded), []
    if remaining < 10000:
        return _won(rounded)
    for size, unit in ((10**12, "조"), (10**8, "억"), (10000, "만")):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count:,}{unit}")
    if remaining:
        parts.append(f"{remaining:,}")
    return ("-" if rounded < 0 else "") + " ".join(parts) + ("원" if remaining else " 원")


def format_fill_telegram_message(fill, *, symbol_names, account, positions):
    """account/positions must describe the state immediately after this fill."""
    code, side, quantity = symbol(fill["symbol"]), fill["side"], fill["quantity"]
    if side not in {"BUY", "SELL"} or type(quantity) is not int or quantity <= 0:
        raise ValueError("BUY/SELL fill with positive integer quantity required")
    name = symbol_names.get(code) or fill.get("name") or "종목명 확인 중"
    if not isinstance(name, str) or len(name) > 200 or any(c in name for c in "\r\n\0"):
        raise ValueError("bounded stock name required")
    signal = fill.get("signal") or {}
    price = _amount(fill["gross"]) / quantity
    held = sum(amount > 0 for amount in positions.values())
    lines = ["🟢 [단타 모의체결 · 매수]" if side == "BUY" else "🔵 [단타 모의체결 · 매도]",
        f"종목: {name} ({code})", f"시각: {instant(fill['at']).astimezone(KST):%Y-%m-%d %H:%M:%S} KST",
        f"수량: {quantity:,}주", f"평균 체결가: {_won(price)}"]
    fees = f"수수료 {_won(fill['fee'])}"
    if side == "BUY":
        lines.append(f"매수 금액: 약 {_korean_won(fill['gross'])} ({fees})")
        reason = " · ".join(REASONS.get(r, r) for r in signal.get("reasons", [])) or "확인 불가"
        score = signal.get("score")
        suffix = f" (조건 충족률 {score}%)" if type(score) is int and 0 <= score <= 100 else ""
        lines.extend([f"진입 사유: {reason}{suffix}", f"체결 후 현금: {_korean_won(account['cash'])} (보유 {held}종목)"])
    else:
        lines.append(f"매도 금액: 약 {_korean_won(fill['gross'])} ({fees} · 세금 {_won(fill['tax'])})")
        pnl = fill.get("realized_pnl")
        lines.append("실현 순손익: 확인 불가" if pnl is None else
                     f"실현 순손익: {_won(pnl, signed=True)}")
        reason = signal.get("exit_reason")
        lines.extend([f"청산 사유: {REASONS.get(reason, reason) or '확인 불가'}",
            f"체결 후 현금: {_korean_won(account['cash'])} · 누적 실현손익: {_won(account['realized_pnl_net'], signed=True)} (보유 {held}종목)"])
    lines.append("※ 모의거래(PAPER ONLY) · 실주문 없음")
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Telegram message length exceeded")
    return message


def _daily_session_date(report, session_date):
    sessions = report['sessions']
    if not sessions:
        raise ValueError('reported session date required')
    day = session_date if session_date is not None else max(sessions)
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day not in sessions:
        raise ValueError('date must identify a reported KST session')
    return day


def _daily_trade_lines(report):
    totals = {}
    for fill in report['fills']:
        code, side, quantity = symbol(fill['symbol']), fill['side'], fill['quantity']
        if side not in {'BUY', 'SELL'} or type(quantity) is not int or quantity <= 0:
            raise ValueError('BUY/SELL fill with positive integer quantity required')
        row = totals.setdefault(code, {'BUY':0, 'SELL':0, 'pnl':Decimal(0), 'name':fill.get('name')})
        row[side] += quantity
        if fill.get('realized_pnl') is not None:
            row['pnl'] += _amount(fill['realized_pnl'])
    lines = []
    for code, row in sorted(totals.items()):
        name = report.get('symbol_names', {}).get(code) or row['name'] or '종목명 확인 중'
        if not isinstance(name, str) or len(name) > 200 or any(c in name for c in '\r\n\0'):
            raise ValueError('bounded stock name required')
        lines.append(f"{name} ({code}) · 매수 {row['BUY']:,}주 / 매도 {row['SELL']:,}주 · 실현 순손익 {_won(row['pnl'], signed=True)}")
    return lines


def format_daily_summary_telegram_message(report, *, session_date=None):
    """Format audited daily account/fill totals without changing the report."""
    day = _daily_session_date(report, session_date)
    account = report['account']
    held = sum(_amount(row['quantity']) > 0 for row in report['positions'].values())
    warning = held or 'UNFLATTENED' in report['sessions'].values()
    capital, realized = _amount(account['initial_capital']), _amount(account['realized_pnl_net'])
    if capital <= 0:
        raise ValueError('positive initial capital required')
    percent = (realized * 100 / capital).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    fees = sum((_amount(fill['fee']) for fill in report['fills']), Decimal(0))
    taxes = sum((_amount(fill['tax']) for fill in report['fills']), Decimal(0))
    pnls = [_amount(trade['realized_pnl']) for trade in report['completed_trades']]
    wins, losses = sum(value > 0 for value in pnls), sum(value < 0 for value in pnls)
    lines = ['⚠️ [단타 모의거래 · 미청산 마감 경고]' if warning else '📊 [단타 모의거래 · 일일 마감 요약]',
        f"날짜: {day} KST · 세션 상태: {report['sessions'][day]}",
        f"초기 자본 {_won(capital)} → 마감 현금 {_won(account['cash'])}"]
    if account['valuation_status'] == 'KNOWN' and account['final_equity'] is not None:
        lines.append(f"마감 평가자산: {_won(account['final_equity'])}")
    else:
        lines.append('마감 평가자산: 평가 불확실')
    lines.extend([f"실현 순손익: {_won(realized, signed=True)} ({percent:+.2f}%)",
        f"총 수수료 {_won(fees)} · 총 세금 {_won(taxes)}",
        f"완결 거래 {len(pnls)}건 (승 {wins} / 패 {losses} / 보합 {len(pnls)-wins-losses}) · 체결 {len(report['fills'])}건"])
    if warning:
        lines.append(f'미청산 보유: {held}종목')
    if not report['fills']:
        lines.append('오늘 체결된 거래가 없습니다.')
    else:
        lines.append('종목별 거래:')
    footer = '※ 모의거래(PAPER ONLY) · 실주문 없음'
    rows = _daily_trade_lines(report)
    # Reserve space for both the omission notice and the mandatory footer.
    # UTF-16 units are conservative for stock names containing supplementary text.
    def size(parts):
        return len(with_channel_header('\n'.join(parts), BotChannel.DAYTRADE).encode('utf-16-le')) // 2
    for index, row in enumerate(rows):
        omitted = len(rows) - index - 1
        tail = [f'… 외 {omitted}종목 생략'] if omitted else []
        if size(lines + [row] + tail + [footer]) > 4096:
            lines.append(f'… 외 {len(rows)-index}종목 생략')
            break
        lines.append(row)
    lines.append(footer)
    if size(lines) > 4096:
        raise ValueError('Telegram message length exceeded')
    return '\n'.join(lines)


def _read_meta(connection, key, default):
    row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _delivery_state(connection, deliveries_key=DELIVERIES_KEY):
    deliveries = _read_meta(connection, deliveries_key, {})
    sent = _read_meta(connection, SENT_IDS_KEY, []) if deliveries_key == DELIVERIES_KEY else []
    if not isinstance(deliveries, dict) or not isinstance(sent, list):
        raise ValueError("invalid notification metadata; sending blocked")
    return deliveries, sent


def _write_meta(ledger, key, value):
    ledger.connection.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))
    ledger.meta[key] = value


def _claim(ledger, event_id, *, deliveries_key=DELIVERIES_KEY):
    with ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        deliveries, sent = _delivery_state(ledger.connection, deliveries_key)
        if event_id in deliveries or event_id in sent:
            return False
        deliveries[event_id] = {"status": "RESERVED", "http_status": None,
                                "bot_channel": BotChannel.DAYTRADE.value, "delivery_policy": DELIVERY_POLICY}
        _write_meta(ledger, deliveries_key, deliveries)
    return True


def _finish(ledger, event_id, result, *, deliveries_key=DELIVERIES_KEY):
    with ledger.connection:
        ledger.connection.execute("BEGIN IMMEDIATE")
        deliveries, sent = _delivery_state(ledger.connection, deliveries_key)
        deliveries[event_id].update(result)
        _write_meta(ledger, deliveries_key, deliveries)
        if deliveries_key == DELIVERIES_KEY and result["status"] == "SENT" and event_id not in sent:
            _write_meta(ledger, SENT_IDS_KEY, sent + [event_id])


def _fill_contexts(ledger):
    # Take one consistent WAL snapshot, then release it before any HTTP call.
    with ledger.connection:
        ledger.connection.execute("BEGIN")
        ledger.audit()
        names = _symbol_names(ledger)
        fills = [json.loads(row[0]) for row in ledger.connection.execute(
            "SELECT f.payload FROM paper_fills f JOIN events e ON e.id=f.event_id ORDER BY e.sequence")]
        intents = {key:json.loads(payload) for key,payload in ledger.connection.execute(
            "SELECT DISTINCT i.id,i.payload FROM events i JOIN paper_fills f ON f.intent_id=i.id")}
    _, _, details = trading_inventory(fills)
    cash, realized, positions, contexts = _amount(ledger.meta["initial_capital"]), Decimal(0), {}, []
    for fill in details:
        code, qty = fill["symbol"], fill["quantity"]
        gross, fee, tax = (_amount(fill[key]) for key in ("gross", "fee", "tax"))
        buying = fill["side"] == "BUY"
        positions[code] = positions.get(code, 0) + (qty if buying else -qty)
        cash += -gross - fee - tax if buying else gross - fee - tax
        realized += fill["realized_pnl"] or 0
        fill["signal"] = intents.get(fill["intent_id"], {}).get("data", {}).get("strategy_signal")
        contexts.append((fill, {"cash":cash, "realized_pnl_net":realized}, dict(positions)))
    return names, contexts


def notify_new_fills(ledger, send_fn):
    """Attempt each unclaimed fill once; only safe status metadata is persisted.

    RESERVED survives a crash between API acceptance and the local SENT commit.
    Such uncertain attempts, and FAILED attempts, require manual reconciliation
    instead of automatic retries that could send duplicate messages.
    """
    if not callable(send_fn):
        raise ValueError("explicit injected sender required")
    names, contexts = _fill_contexts(ledger)
    results = []
    for fill, account, positions in contexts:
        event_id = fill["event_id"]
        if not _claim(ledger, event_id):
            continue
        result = {"status":"FAILED", "http_status":None}
        try:
            text = format_fill_telegram_message(fill, symbol_names=names, account=account, positions=positions)
            response = send_fn(with_channel_header(text, BotChannel.DAYTRADE))
            if isinstance(response, dict):
                http = response.get("http_status")
                result["http_status"] = http if type(http) is int and 100 <= http <= 599 else None
                if response.get("status") == "SENT" and http == 200:
                    result["status"] = "SENT"
        except Exception:
            # Exception messages may contain token-bearing URLs or chat IDs.
            pass
        _finish(ledger, event_id, result)
        results.append({"event_id":event_id, **result})
    return results


def notify_daily_summary(ledger, report, send_fn, *, session_date=None):
    """Reserve the KST date before I/O; FAILED/RESERVED dates never auto-retry."""
    if not callable(send_fn):
        raise ValueError('explicit injected sender required')
    day = _daily_session_date(report, session_date)
    if not _claim(ledger, day, deliveries_key=DAILY_SUMMARY_KEY):
        return []
    result = {'status':'FAILED', 'http_status':None}
    try:
        text = format_daily_summary_telegram_message(report, session_date=day)
        response = send_fn(with_channel_header(text, BotChannel.DAYTRADE))
        if isinstance(response, dict):
            http = response.get('http_status')
            result['http_status'] = http if type(http) is int and 100 <= http <= 599 else None
            if response.get('status') == 'SENT' and http == 200:
                result['status'] = 'SENT'
    except Exception:
        # Token-bearing transport URLs and response bodies never enter metadata.
        pass
    _finish(ledger, day, result, deliveries_key=DAILY_SUMMARY_KEY)
    return [{'session_date':day, **result}]
