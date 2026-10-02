# Decision 0001: KRX Open API for Daily Market Data

Date: 2026-07-24

Status: Accepted

## Decision

Use KRX Open API as the first provider for daily OHLCV collection.

## Reason

The current project needs verified daily prices and ATR before it can create price-based signals. KRX provides an official daily-market-data path without making brokerage-account integration part of the first milestone.

## Consequences

- KRX credentials remain outside Git.
- The first ingestion interface targets daily OHLCV, not real-time quotes or orders.
- KIS remains outside the current scope and can be added as a separate adapter if real-time or brokerage functions are approved.
- Price signals remain disabled until OHLCV history and ATR pass their validation gates.
