# Hermes Stock Investment Assistant Architecture

## Outcome

The stock engine is a channel-neutral, deterministic research service. Official Hermes Desktop, Telegram, and scheduled jobs call the same versioned JSON contract. The existing Telegram gateway is not treated as a Hermes backend.

## Boundaries

```text
KRX / DART / approved news sources / optional Toss read-only API
  -> provider adapters
  -> normalized point-in-time records
  -> deterministic screening and event-study backtest
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

## Hard Separation

- `stock_assistant` owns contracts, validation, calculations, screening, backtests, portfolio state, approval state, and report IDs.
- Hermes profiles own interpretation, counterarguments, and presentation. They cannot write the WIKI or place orders.
- The Telegram adapter owns transport and allow-list checks. It does not calculate investment signals.
- The WIKI writer accepts only approved journal payloads and never receives arbitrary paths.
- Brokerage order APIs are outside the MVP.

## Data Freshness

- KRX Open API is the baseline for official daily KOSPI, KOSDAQ, and ETF data.
- DART is the baseline for filings and financial statements.
- Toss Securities may later provide authenticated current/real-time prices and read-only holdings. It is optional and disabled by default.
- Mirae Asset Securities holdings are manual until a verified official securities API is selected.
- Intraday alerts remain unavailable when no authenticated real-time provider is configured.

## Repository Layout

```text
src/stock_assistant/       domain and application code
tests/                     network-free tests
tests/fixtures/            sanitized external-response samples
hermes/profiles/           profile templates without secrets
docs/contracts/            JSON contracts and reason codes
docs/findings/              verified environment findings
scripts/verify.ps1         single verification entrypoint
```

## Completion Claims

Local tests prove only deterministic behavior against fixtures. Live collection, Desktop connectivity, Telegram delivery, WIKI persistence, and scheduled operation each require separate operational evidence.

