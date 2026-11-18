---
name: investment-committee-review
description: Use for Korean stock reviews. Consolidate CIO findings through the actual channel-specific specialist tools.
---

# Investment Committee Review

Use this skill for a Korean-stock investment-committee request that combines market/supply, company/financial-disclosure, and risk/counterargument work into a single CIO result. The stock service remains authoritative for stored reports, calculations, report IDs, holdings, and permissions; this skill governs orchestration and delivery.

## Telegram actual review — TELEGRAM_EXPERT_REVIEW_V1

Telegram does not expose the Desktop Bot Chat `message_agent` tool. Use the CIO's `stock_experts` MCP server and its `stock_expert_review` tool exactly once. Discover the tool with `tool_search` if deferred; use the exact registered name. The structured bridge runs the actual `stock-market`, `stock-fundamentals` and `stock-risk` profiles once each in parallel. Do not substitute `delegate_task`, terminal commands, `execute_code` or an imagined committee.

For a stored report with a known ID, go directly to this tool with `mode=stored`, the user's `question`, exact `report_id` and requested six-digit `symbols` (at most five). Do not add `evidence`. The bridge retrieves and validates the same saved report; CIO must not first repeat HTTP/StockClient/exact-ID requests, inspect source code or read production databases. If lookup or the tool fails, state the specific unavailable work without retrying via those routes. Never use another report in place of the requested ID.

For fresh company, holding or prospective timing research, collect the material original evidence first and use `mode=research`, `holding` or `timing` respectively, with `evidence.observed_at` and `evidence.sources` entries containing `kind` and original `data`. Preserve URLs, periods, CFS/OFS, units and observation times. Never pass credentials or account identifiers. Missing prices, theme-cycle evidence or technical conditions remain unverified.

Synthesize only actual `COMPLETE` specialist memos. For `PARTIAL` or `UNAVAILABLE`, identify the missing specialist and do not claim three-way agreement. Display the actual market/financial/risk completion status briefly, followed by one CIO synthesis. No second debate, specialist Telegram send or interim completion announcement.

Respect the user's stages: first screening, second screening, news/company research, theme-cycle confirmation, then technical entry/exit review. Do not invent either screening definition, a theme period, stop percentage, position size or an unobserved technical price. Separate a promising company from a confirmed current entry. Preserve saved `VERIFY_PENDING`, scores, ranking, rules and paper-account policy. Loss recovery or urgency is not an entry rationale.

## Procedure

1. Classify the request as either **retrieve** or **refresh**. For “latest,” “saved,” or “current saved” candidates, retrieve the latest stored report only; generate a new candidate report only when the user explicitly asks to refresh, regenerate, or run a new analysis. A pure report display does not automatically require expert work.
2. Establish one `report_id` for a stored-report meeting. When expert work is needed, use the channel-specific route above; in a canonical Desktop Bot Chat use `message_agent` in parallel, at most once each, with the same `report_id`, a clearly bounded scope, and an instruction to return one memo without spawning or mentioning other agents.
3. Verify the report ID, as-of timestamp, ruleset, candidate symbols and unavailable fields. Telegram's structured bridge performs the exact stored-report retrieval; do not duplicate its lookup before or after the call. Never silently replace missing or future-dated data with a guessed value.
4. Wait for the returned memos, then hold one CIO decision round. Do not create a second expert debate, re-verification round, or fresh report after the memos arrive unless the user explicitly starts a new analysis.
5. Separate **confirmed facts**, **CIO inference**, **user assumptions**, and **확인 불가** data. Do not turn a positive annual OCF into a TTM OCF claim, and do not treat missing dilution or management-history data as evidence that no risk exists.
6. Make the company-level decision and its reasons explicit for every name. Preserve a stored decision such as `VERIFY_PENDING` exactly, with its missing checks; do not flatten it into a generic buy/hold label. A review candidate or deferred entry must state the evidence required and the condition that would invalidate the thesis.
7. In a Desktop committee room the actual expert messages are already visible; deliver the CIO judgment once without replaying the memos. For Telegram, send the CIO synthesis and actual specialist completion status once unless the user asks for meeting details. Do not print `@user`, `@cio`, or stock-role mentions in the memos or final judgment.
8. If the room already contains the complete expert meeting and CIO result and there is no new question, claim, handoff, or result to report, answer exactly `(pass)` rather than repeating the report.
9. If the user asks sell versus hold or otherwise implies a position, check the CIO holdings endpoint when it materially affects the action. An absent symbol is not proof that the user has no position; label quantity and average price as `확인 불가` and separate the company thesis from the holder action. Do not force new-entry conditions into a sell conclusion or append unsolicited trading scenarios to a retrospective review. If a journal entry is requested, prepare a preview only; never record it without one-time user approval. Never execute orders or request brokerage credentials.

## Decision discipline

- Identical scores are not a ranking; explain which common gates they represent and what differentiates the names.
- Treat real-time market data, contract execution details, unofficial catalysts, and incomplete filing histories as unavailable unless the source explicitly confirms them.
- Keep a fresh intraday quote in a separate overlay for current sell/hold questions; use it to state the observation and current-price condition only, never to rewrite a stored report, report ID, saved close, or candidate rank.
- A loss, cost basis, or hoped-for recovery is not a reason to average down; revalidate the thesis and its confirmation conditions.
- Keep a stored-report review read-only. Use exact concrete dates and the report's as-of time when relative timing could be misunderstood.

## References

- `references/group-chat-delivery.md` contains the reusable meeting-output template and channel decision table.
