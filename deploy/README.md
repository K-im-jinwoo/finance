# Candidate Deployment

This Compose service is an inactive candidate. It publishes port 9120 only on Oracle loopback and also joins the existing private Docker network. It contains no brokerage order endpoint. A native Hermes process on Oracle uses `http://127.0.0.1:9120`; no public firewall opening is required.
The candidate uses the dedicated Compose project name `stock-assistant`, so it cannot become part of the existing `deploy` project that owns the Telegram and WIKI containers.

## Host inputs

```text
/srv/stock-assistant/state/
/srv/stock-assistant/stock-api-roles.json
/srv/stock-assistant/krx-auth-key
/srv/stock-assistant/dart-api-key
```

Create a JSON object containing unique random secrets of at least 32 characters for `cio`, `market`, `fundamentals`, `risk`, and `scheduler`. Store it only in the role-secret file, set mode `0600`, and make it readable by the configured container UID. Do not copy values into the image, repository, WIKI, prompt, or logs. Specialists can analyze and retrieve reports but cannot read or change holdings or journal drafts.

Store the KRX and OpenDART keys as one-value files with the same permissions. They are mounted read-only for deterministic ingestion commands and are never passed in command-line arguments or model prompts.

## Data and report cycle

After the first approved 120-calendar-day backfill, `deploy/jobs/run_stock_cycle.sh` supports three outputs:

- `morning`: read the last completed market dataset, refresh DART dilution and title-level research signals through the last fully observable date, and generate the five-candidate report.
- `evening`: ingest one KRX date, enrich the technical shortlist with the approved DART business year, dilution history, and title-level research signals, then generate the report.
- `weekly`: freeze newly mature 5·20·60-session virtual outcomes for all prior reports, refresh missing annual DART data and disclosures, then generate the report without repeating the KRX backfill.

The script prints only the final report so Hermes cron can deliver it to Telegram. Set `STOCK_DART_BUSINESS_YEAR` explicitly; do not infer a fiscal year during unattended operation.
Completed virtual outcomes are immutable. Pending horizons remain explicit and are excluded from return and win-rate aggregates.

## Static validation

```bash
docker compose --env-file deploy/candidate.env.example -f deploy/compose.yaml --profile candidate config --quiet
```

## Candidate smoke gate

Run only after deployment approval:

1. Build the image without starting the existing Telegram or WIKI services.
2. Start `stock-assistant` on the private network.
3. Confirm `/health` reports `orders_enabled: false`.
4. Send a fixture-equivalent `/v1/screen` request with a specialist secret.
5. Confirm an unauthenticated `/v1/holdings` request returns 401 and `/v1/orders` returns 403.
6. Confirm a specialist-authenticated `/v1/holdings` request returns 403.
7. Confirm a CIO-created report is retrievable by the same report ID through the specialist read route.
8. Confirm no WIKI, Telegram, n8n, or brokerage state changed.

The first seven checks are implemented by `deploy/smoke_candidate.py`; it reads role tokens from the mounted host file and never prints them.

KRX and OpenDART key-file mounts are included in the inactive candidate, but live calls are not yet validated. Toss credentials and brokerage connectivity remain outside this candidate.
