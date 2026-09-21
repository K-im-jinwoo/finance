---
name: stock-research
description: Read-only Korean stock screening, report retrieval, portfolio review, and approved journal preview through the private stock service.
---

# Stock Research

Use this skill for Korean common stocks and ordinary ETFs. The private stock service is authoritative for calculations, report IDs, holdings, and permission checks.

## Required environment

- `STOCK_API_BASE_URL`: private service origin, normally `http://stock-assistant:9120`
- `STOCK_API_ROLE_TOKEN_FILE`: a file containing only this profile's token

Never print, copy, summarize, or send the token. Never read the server's full role-secret JSON.

## Workflow

1. Use `StockClient` from `stock_assistant.client`; do not reproduce indicators or rankings in the model.
2. Keep confirmed facts, inferences, assumptions, and unavailable data separate.
3. Cite the returned report ID in every Desktop or Telegram answer.
4. Retrieve an existing report by ID before discussing it on another channel.
5. For weekly review, retrieve `/v1/performance/{report_id}` and distinguish completed horizons from `PENDING_ENTRY` or `PENDING_HORIZON`. Never present a pending value as a return.
6. Compare outcomes by `ruleset_version`, decision, and horizon. Do not change a rule automatically from a small or incomplete sample.
7. Treat `403` as a hard role boundary. Do not retry with another profile or secret.
8. A loss is never an automatic averaging-down signal. Revalidate the thesis and additional-check conditions.
9. Never invoke, design around, or claim access to brokerage order endpoints.

Use `render_candidate_report` from `stock_assistant.presentation` when the same report must be shown in a channel with a message-size limit.
