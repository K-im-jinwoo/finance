# Hermes Stock Investment Assistant Architecture

## Outcome

The stock engine is a channel-neutral, deterministic research service. Official Hermes Desktop, Telegram, and scheduled jobs call the same versioned JSON contract. The existing custom Telegram gateway is not treated as a Hermes backend.

## Data Flow

```text
KRX / DART / approved news sources / optional Toss read-only API
  -> provider adapters and boundary validation
  -> normalized point-in-time records and idempotent storage
  -> deterministic indicators, screening and event-study backtest
  -> evidence bundle
  -> Hermes investment profiles (analysis only)
  -> Desktop or Telegram presentation

manual holdings
  -> portfolio and thesis store
  -> deterministic risk checks
  -> scenario report

explicit one-time approval
  -> investment journal writer
  -> WIKI-approved path only
```

The repository also keeps sanitized, inactive snapshots of the legacy n8n news workflows. They are evidence and migration inputs, not production-ready workflows.

## Hard Separation

- `stock_assistant` owns contracts, validation, calculations, screening, backtests, portfolio state, approval state, and report IDs.
- Hermes profiles own interpretation, counterarguments, and presentation. They cannot write the WIKI or place orders.
- The Telegram adapter owns transport and allow-list checks. It does not calculate investment signals.
- The WIKI writer accepts only approved journal payloads and never receives arbitrary paths.
- Brokerage order APIs are outside the MVP.
- Gemini, when retained for news sentiment, cannot calculate entry, target, or stop prices.
- Oracle binds the stock API to host loopback only. Native Hermes profiles call that loopback endpoint with separate role-token files; the complete role-token map remains available only to the stock service.

## Data Freshness

- KRX Open API is the baseline for official daily KOSPI, KOSDAQ, and ETF data.
- DART is the baseline for filings and financial statements.
- Toss Securities provides optional authenticated read-only current prices and one-minute candles for holdings plus the latest five candidates. It is disabled until a credential-gated live smoke passes.
- Mirae Asset Securities holdings are manual until a verified official securities API is selected.
- Intraday alerts remain unavailable when no authenticated real-time provider is configured.

## Repository Layout

```text
src/stock_assistant/       domain and application code
tests/                     Python and Node network-free tests
tests/fixtures/            normalized provider samples
hermes/profiles/           profile templates without secrets
workflows/n8n/             sanitized inactive legacy snapshots
scripts/lib/               workflow sanitizer and audit libraries
docs/                      decisions, contracts, findings and plans
scripts/verify.ps1         single verification entrypoint
```

## Remaining Legacy Workflow Risks

The n8n snapshots still require separate fixes for hard-coded dates, empty-news behavior, Gemini error visibility, deterministic signal formulas, and verified database constraints before any import or activation.

## Completion Claims

Local tests prove deterministic behavior against fixtures and workflow safety checks. They do not prove live KRX/DART collection, production PostgreSQL state, Desktop connectivity, Telegram/Slack delivery, WIKI persistence, n8n execution, or scheduled Oracle operation.
