# Candidate Deployment

This Compose service is an inactive candidate. It exposes port 9120 only to the existing private Docker network and contains no brokerage order endpoint.

## Host inputs

```text
/srv/stock-assistant/state/
/srv/stock-assistant/stock-api-shared-secret
```

Generate a random shared secret on the host, store only the value in the secret file, set mode `0600`, and make it readable by the configured container UID. Do not copy it into the image, repository, WIKI, prompt, or logs.

## Static validation

```bash
docker compose --env-file deploy/candidate.env.example -f deploy/compose.yaml --profile candidate config --quiet
```

## Candidate smoke gate

Run only after deployment approval:

1. Build the image without starting the existing Telegram or WIKI services.
2. Start `stock-assistant` on the private network.
3. Confirm `/health` reports `orders_enabled: false`.
4. Send a fixture-equivalent `/v1/screen` request with the shared secret.
5. Confirm an unauthenticated `/v1/holdings` request returns 401 and `/v1/orders` returns 403.
6. Confirm no WIKI, Telegram, n8n, or brokerage state changed.

KRX, DART, and Toss credentials are not part of this candidate container yet. The provider adapters are code- and fixture-validated only.

