# Stock Automation Architecture

## Current Foundation Scope

This repository versions sanitized n8n workflow exports and the local verification harness. It does not yet contain a verified PostgreSQL schema snapshot, KRX network integration, ATR implementation, or production deployment configuration.

## Intended Data Flow

```text
KRX daily OHLCV
  -> boundary validation
  -> normalized idempotent storage
  -> minimum-history check
  -> Wilder ATR

Naver news
  -> normalization and deduplication
  -> Gemini sentiment validation
  -> validated OHLCV and ATR lookup
  -> deterministic signal calculation
  -> PostgreSQL
  -> Slack notification
```

## Source of Truth

- Harness design: `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`
- Provider decision: `docs/decisions/0001-krx-open-api.md`
- Repository workflow copies: `workflows/n8n/`
- Full verification command: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`

Production n8n state and production PostgreSQL state remain external and must be revalidated before any operational change.

> **Safety boundary:** The files under `workflows/n8n/` are sanitized, inactive
> repository snapshots, not production-ready workflows. Date handling, empty-news
> behavior, Gemini error handling, and price-proposal hardening remain follow-up
> work and must be verified before activation.
