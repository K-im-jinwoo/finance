from urllib.parse import urlencode, urlsplit

def drafts(result: dict, base_url: str | None = None):
    """Prepare stable deep links and text. Deliberately contains no send function."""
    if base_url:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("approved HTTPS dashboard origin required")
    eid = result["experiment_id"]
    items = []
    summaries = set()
    for event in result["state"]["outbox"]:
        if event["kind"] == "EVENING_SUMMARY":
            day = event["detail"]["trade_date"]
            if day in summaries:
                continue
            summaries.add(day)
        query = {"experiment": eid, "event": event["event_id"]}
        detail = event.get("detail")
        if isinstance(detail, dict) and detail.get("trade_id"):
            query.update(trade=detail["trade_id"], report=detail["report_id"])
        path = "/?" + urlencode(query)
        lines = [f"[자동 모의계좌 / {result['coverage']['mode']}] {event['kind']} {event.get('symbol') or ''}", f"실험 {eid}", f"기준시각 {event['at']}"]
        if event["kind"] == "EVENING_SUMMARY":
            equity = detail.get("equity", {})
            lines += [f"총자산 {equity.get('total_assets') or '확인 불가'}원 · 계좌수익률 {equity.get('return') or '확인 불가'}", f"현금 {equity.get('cash') or '확인 불가'}원 · 계산상태 {detail.get('status', '확인 불가')}", "공시 무효화: 확인 필요 / 자동 미적용"]
        elif event["kind"] in {"BUY", "SELL"}:
            lines += [f"가상 체결 {detail['quantity']}주 / {detail['price']}원 · 비용 {detail['fee']}원", f"신호 {detail['signal_at']} → 체결 {detail['filled_at']}", f"원본 보고서 {detail['report_id']}"]
        else:
            lines.append("원인·증거와 판정시각은 상세 기록에서 확인")
        items.append({**event, "detail_path": path, "detail_url": base_url.rstrip("/") + path if base_url else None,
                      "delivery": "DISABLED_PENDING_APPROVAL", "link_status": "PUBLIC_ORIGIN_NOT_CONFIGURED" if not base_url else "LOGIN_REQUIRED",
                      "text": "\n".join(lines)})
    return items

