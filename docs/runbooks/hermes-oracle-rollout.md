# Official Hermes and Stock Assistant Oracle Rollout

Status: Gate 2 deployed; Gate 3 Desktop SSH reachable; Gate 4 profiles, independent OAuth, Bot Chats, and CIO-to-specialist DM smoke complete; visible Desktop group room and Gates 5-6 pending

## Gate 1 - Inputs

- KRX Open API key
- OpenDART API key
- optional Toss Securities read-only OAuth credential
- separate test Telegram Bot token and allowed user ID
- approved remote authentication: Tailscale is recommended; Nous OAuth is the public-internet alternative
- approved Oracle backup and rollback window

## Gate 2 - Stock API candidate

Completed on 2026-09-21 with release `08c10af`. The loopback health and 401/403/report round-trip smoke passed. See `docs/reports/2026-09-21-oracle-candidate-deployment.md`.

Deploy `deploy/compose.yaml` with port 9120 published only to Oracle loopback. Keep the existing private Docker network for future internal consumers and verify the candidate smoke gate in `deploy/README.md`. Give each Hermes profile only its own role-token file; never mount the complete role-secret JSON into Hermes.

Rollback: stop and remove only the `stock-assistant` candidate container. Preserve `/srv/stock-assistant/state` for diagnosis.

After the candidate API smoke passes, perform the first data run in this order:

1. Run one 120-calendar-day KRX backfill with the mounted KRX key file.
2. Inspect saved security counts, trading-day counts, excluded asset classes, and the latest date before continuing.
3. Run OpenDART enrichment for an explicitly approved business year and the technical shortlist only; reconcile detailed financing receipts with filing-list receipt dates.
4. Generate five candidates and confirm every unavailable source is labeled rather than inferred.
5. Enable `deploy/jobs/run_stock_cycle.sh` modes one at a time: `morning`, `evening`, then `weekly`.

Stop the cycle if a provider schema changes, the last trading date does not advance as expected, or a secret value appears in output.

## Gate 3 - Official Hermes

Install the official Hermes runtime as a separate service. Do not replace the existing custom Telegram gateway. Prefer Desktop's SSH connection to a loopback `hermes serve` backend for the first validation. If a remote URL is later exposed through Tailscale or the public internet, apply the official authentication guidance; never expose username/password authentication directly to the public internet.

Installed on 2026-09-21 as `v0.21.3`, then updated to main commit `afc3b7c6f397c6d21fd2c129ca17b18b544dd8dc` to match the Windows Desktop build and use loopback PKCE OAuth. The loopback SSH session-token and owner-nonce smoke passed without leaving port 9119 publicly listening. Windows Desktop was built, launched, and reported the Oracle connection as `Reachable`. See `docs/reports/2026-09-21-hermes-runtime-desktop-bootstrap.md`.

Register the SSH connection in Desktop with `ubuntu@144.24.92.159:22`, remote Hermes path `/home/ubuntu/.local/bin/hermes`, and the approved local SSH key. Gate 3 remains incomplete until Desktop's own `Test` reports `Reachable`, reconnect works, and one authenticated model conversation survives a restart.

Verify `/api/status`, one normal chat, reconnect after restart, and Desktop session continuity before adding profiles.

## Gate 4 - Profiles

Create `stock-cio`, `stock-market`, `stock-fundamentals`, and `stock-risk` as independent profiles. Apply the repository SOUL templates, configure `terminal.cwd`, and give each profile its own model authentication. Do not clone Telegram tokens or OAuth refresh tokens between profiles.

The read-only Toss rollout adds `stock-live-market` as a fifth internal profile. It receives independent model authentication and the bounded `stock-intraday.sh` wrapper, but no Telegram token and no stock API role token. The CIO delegates at most one 1-to-5-symbol snapshot when a request or final-decision condition needs current price or intraday volume. Pure stored-report reviews do not invoke it, and its output remains a separate overlay outside the report ID.

Official Hermes exposes `message_agent` only in canonical Bot Chats. Keep Desktop on that path. For Telegram, install `stock-cio/scripts/stock-live-market-query.sh` in the CIO profile; it validates one to five symbols and synchronously invokes the independent live-market Bot Chat with a fixed prompt and a 65-second process timeout. Do not substitute top-level `delegate_task`: it is intentionally asynchronous and would create an interim response plus a later completion response.

Configure the CIO gateway to suppress Telegram-only interim chatter while preserving its final response:

```yaml
display:
  platforms:
    telegram:
      tool_progress: off
      interim_assistant_messages: false
      long_running_notifications: false
      streaming: false
```

Restart only the CIO gateway after this configuration change. Verify one Telegram request produces exactly one final overlay and no `확인 중` or `진행 중` message.

Only the CIO profile receives the test Telegram Bot. Specialist profiles remain Desktop/Bot Mode participants and can be delegated work by the CIO.

Verified on 2026-09-21:

- All four profiles own one independent `openai-codex` OAuth credential and use `gpt-5.6-sol`.
- Each profile has its own role token, SOUL, stock-research skill, Bot Mode metadata, and canonical `Bot Chat`.
- The CIO can dispatch to all three specialists with `message_agent` and collect the three replies in one run.
- A delegated specialist returns through the current call instead of sending a second `message_agent` reply; this avoids a `target_busy` collision while the CIO turn is still active.
- Orders return `403` for every profile. Specialist holdings return `403`; CIO holdings return `200`.

Gate 4 still requires one visible Desktop group room with all four profiles and a restart-continuity check. The CLI DM smoke does not prove the group-chat UI.

The fifth `stock-live-market` profile is specified in the repository but is not covered by the 2026-09-21 four-profile verification. Create it, authenticate it, copy its SOUL, policy, and wrapper, then verify one delegated fresh quote and one invalid-symbol rejection before marking the extension deployed.

## Gate 5 - Channels and schedules

Use the test Telegram Bot while the existing production receiver remains unchanged. Validate the same report ID in Desktop and Telegram. Then create KST jobs for weekday 08:30, weekday 20:00, and Saturday 12:00. Delivery success and analysis success remain separate statuses.

Rollback: stop only the CIO test gateway and stock cron jobs. The current WIKI/n8n/Telegram service remains untouched.

## Gate 6 - WIKI

Mount or connect the personal WIKI only after preview and one-time approval tests pass. The writer is limited to `wiki/20_Areas/Investments/*.md`. Perform one disposable test under a temporary WIKI root first. Actual WIKI writes require a separately approved transaction.

## Explicitly deferred

- brokerage orders and conditional orders
- production Telegram cutover
- live n8n workflow import or activation
- automatic rule changes based on backtest output
