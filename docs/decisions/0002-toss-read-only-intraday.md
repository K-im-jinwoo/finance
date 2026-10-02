# Decision 0002: Toss Securities for Limited Read-only Intraday Data

Date: 2026-09-22

Status: Accepted for implementation; live credential smoke remains pending.

## Decision

Use the official Toss Securities Open API only for read-only current prices and one-minute candles.
Poll at most every five minutes and only for the union of manually stored holdings and the latest five candidate symbols.

## Boundaries

- Keep KRX Open API as the authoritative daily OHLCV source for screening and backtests.
- Treat Toss timestamps as provider timestamps and preserve the separate observation timestamp.
- Label quotes as fresh, delayed, or stale; never present a stored close as a current price.
- Exclude account, holdings, buying-power, order, conditional-order, and personal-order-event APIs.
- Do not add `X-Tossinvest-Account` to market-data requests.
- Do not change candidate decisions from an intraday signal alone.
- Do not alert on an unfinished one-minute candle.
- Keep client credentials and access tokens outside Git and logs.

## Hermes orchestration

- Use a dedicated internal `stock-live-market` profile for on-demand Toss observations; do not add another user-facing Telegram bot.
- The CIO delegates once with one to five explicit six-character symbols only when the request or final-decision condition needs current price or intraday volume.
- Keep the observation as an external real-time overlay. It does not modify the stored report ID, candidate rank, or deterministic daily calculation.
- A failed, delayed, or stale observation limits only the overlay. It does not erase the confirmed stored-report evidence, but any action that requires a fresh quote remains `매수 보류`.

## Rollout Gates

1. Network-free fixture and boundary tests pass.
2. The user issues a Toss Open API client ID and client secret and allow-lists the Oracle public IP.
3. Credentials are installed as root-readable Docker secrets without printing them.
4. One-symbol live smoke verifies timestamp, price, currency, and candle geometry.
5. A paused five-minute job is manually run and its Telegram delivery is verified before activation.
