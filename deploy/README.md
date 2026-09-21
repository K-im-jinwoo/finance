# Candidate Deployment

This Compose service is an inactive candidate. It publishes port 9120 only on Oracle loopback and also joins the existing private Docker network. It contains no brokerage order endpoint. A native Hermes process on Oracle uses `http://127.0.0.1:9120`; no public firewall opening is required.

## Host inputs

```text
/srv/stock-assistant/state/
/srv/stock-assistant/stock-api-roles.json
```

Create a JSON object containing unique random secrets of at least 32 characters for `cio`, `market`, `fundamentals`, `risk`, and `scheduler`. Store it only in the role-secret file, set mode `0600`, and make it readable by the configured container UID. Do not copy values into the image, repository, WIKI, prompt, or logs. Specialists can analyze and retrieve reports but cannot read or change holdings or journal drafts.

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

KRX, DART, and Toss credentials are not part of this candidate container yet. The provider adapters are code- and fixture-validated only.
