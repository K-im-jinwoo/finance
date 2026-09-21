# Official Hermes and Stock Assistant Oracle Rollout

Status: prepared, not executed

## Gate 1 - Inputs

- KRX Open API key
- OpenDART API key
- optional Toss Securities read-only OAuth credential
- separate test Telegram Bot token and allowed user ID
- approved remote authentication: Tailscale is recommended; Nous OAuth is the public-internet alternative
- approved Oracle backup and rollback window

## Gate 2 - Stock API candidate

Deploy `deploy/compose.yaml` with port 9120 published only to Oracle loopback. Keep the existing private Docker network for future internal consumers and verify the candidate smoke gate in `deploy/README.md`. Give each Hermes profile only its own role-token file; never mount the complete role-secret JSON into Hermes.

Rollback: stop and remove only the `stock-assistant` candidate container. Preserve `/srv/stock-assistant/state` for diagnosis.

## Gate 3 - Official Hermes

Install the official Hermes runtime as a separate service. Do not replace the existing custom Telegram gateway. Prefer Desktop's SSH connection to a loopback `hermes serve` backend for the first validation. If a remote URL is later exposed through Tailscale or the public internet, apply the official authentication guidance; never expose username/password authentication directly to the public internet.

Verify `/api/status`, one normal chat, reconnect after restart, and Desktop session continuity before adding profiles.

## Gate 4 - Profiles

Create `stock-cio`, `stock-market`, `stock-fundamentals`, and `stock-risk` as independent profiles. Apply the repository SOUL templates, configure `terminal.cwd`, and give each profile its own model authentication. Do not clone Telegram tokens or OAuth refresh tokens between profiles.

Only the CIO profile receives the test Telegram Bot. Specialist profiles remain Desktop/Bot Mode participants and can be delegated work by the CIO.

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
