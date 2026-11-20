# Daily research data and report recovery

The scheduled morning, evening and weekly cycles refresh daily prices before
analysis. The target is the previous business day verified by the existing Toss
market-calendar API. This avoids interpreting a holiday or an unpublished
same-day KRX response as a successful update.

`refresh-daily` starts after the oldest stored market date, requests only missing
sessions, and bounds recovery to 31 calendar days by default. KOSPI, KOSDAQ and
ETF responses must all contain the requested trading date before the day is
saved. Empty trading-day data, authentication errors and unknown calendars fail
the cycle; they are not treated as holidays. A wider backlog requires reviewed
backfill rather than silently increasing the request window.

The existing shared Toss token cache is used whenever
`STOCK_TOSS_AUTH_CACHE_DIR` is configured. The installed adapter at
`/opt/stock-shared-auth` must support `calendar`; missing shared authentication
fails instead of issuing an independent token.

Only `refresh-daily` wraps calendar and KRX GET requests with timeout recovery.
The initial request plus at most two retries use the existing 15-second socket
timeout, with waits of 2 and 5 seconds. Only a real `TimeoutError`, including one
wrapped in `URLError`, qualifies. Authentication errors, HTTP failures (including
429), schema errors and invalid or empty trading data still fail immediately.
Token issuance and the full report cycle are not retried.

Failed attempts emit structured JSON to stderr with the fixed public API path,
provider, market, validated request date, attempt count and transport timing.
`CONNECTION_OR_HEADERS` covers opening the connection and waiting for response
headers; the existing opener cannot distinguish those two phases. `RESPONSE_BODY`
identifies a timeout while reading the body. Status and JSON-decoding failures
have separate phases. Headers, credentials, arbitrary query values and response
bodies are never included. The terminal request context is also saved in the
existing daily readiness JSON and included in the CLI failure output. Retried
success remains silent on stdout until the normal result is produced.

Schema 9 adds `daily_data_checks` without removing existing tables. A successful
check records its real observation time and verified daily date. After readiness
tracking is enabled, new reports require a successful check on the same KST day
and that daily date for every selected candidate. Historical reports remain
readable. Historical analyses before the first readiness record retain the
existing interface and explicitly show that freshness was not verified.

The report fact summary displays the daily input date separately from the report
analysis time. Candidate ranking and intraday alert thresholds are unchanged.

Scheduled cycles emit `STOCK_CYCLE status=FAILED stage=... exit=...` for errors
and `status=READY` only after report generation succeeds. Configure only the
three stock report jobs with Hermes `no_agent=true`, preserving their schedule
and Telegram destination, so script failure determines the job failure status.
The deterministic stored report is the delivered output.

The morning job uses `10 8 * * 1-5` in the CIO profile's KST schedule. The
host wrapper retries only a daily provider timeout or unavailable KRX data
at 08:25 and 08:45. The daily refresh must finish successfully for all three
markets before 08:50 KST. A failed refresh at the cutoff produces one error
notification. Authentication and other terminal errors fail immediately.
Hermes runs the report once in the same execution after the refresh succeeds.
Keep the Hermes script timeout long enough for the collection window and
report generation. The former 07:00 prefetch timer can be disabled once this
job is active.

Run `sh /home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-morning.sh --once`
for an operator-initiated refresh and report outside the window. The script
writes the report to stdout. Hermes handles Telegram delivery for its jobs.

## Verification and rollout

1. Run the repository's full `scripts/verify.ps1` with Python 3.12+.
2. Preserve the current release, image, runtime environment and mounts. Back up
   the SQLite database through its backup API and verify backup integrity.
3. Build an image overlay from the running image with only verified source
   changes; keep the existing shared-auth mounts and settings.
4. Update the shared-auth adapter atomically with a recoverable source backup.
5. Run the actual scheduled scripts through the Hermes execution path without
   invoking message delivery for the recovery verification. Verify daily dates,
   report ID, persisted report, service health and `orders_enabled=false`.
6. Observe the next existing scheduled executions. Direct script verification
   does not establish that future timer dispatch or Telegram delivery succeeded.

Rollback restores the release pointer, image configuration and shared-auth
adapter. Do not erase newly observed market data or old reports. Leave any
readiness failure visible until it has been investigated.
