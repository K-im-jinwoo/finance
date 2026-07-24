# Stock Automation Agent Guide

## Start Here

- Architecture: `ARCHITECTURE.md`
- Approved harness design: `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`
- Active implementation plans: `docs/superpowers/plans/`
- Provider decisions: `docs/decisions/`
- Sanitized n8n exports: `workflows/n8n/`

## Commands

- Full verification: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`
- Node tests: `npm.cmd test`
- Workflow audit: `npm run audit:workflows`

## Rules

- Use KRX Open API as the first daily OHLCV provider.
- Keep Gemini limited to sentiment and market-scenario analysis.
- Do not calculate or store entry, target, or stop prices without validated OHLCV and ATR.
- Never commit credentials, API keys, tokens, passwords, or production data.
- Keep committed n8n workflows inactive and sanitized.
- Treat n8n activation, production database changes, real Slack sends, and external pushes as approval-required actions.
- Parallel agents may perform read-only audits, but writes to the same workflow, migration sequence, or file must be serialized.
- Do not claim completion without fresh output from the full verification command.
