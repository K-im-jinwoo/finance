# Stock Investment Assistant Guide

## Start Here

- Architecture: `ARCHITECTURE.md`
- Approved stock assistant requirements: `docs/superpowers/specs/2026-09-21-hermes-stock-investment-assistant.md`
- Approved harness design: `docs/superpowers/specs/2026-07-24-stock-automation-harness-design.md`
- Phase 0 findings: `docs/findings/2026-09-21-phase-0.md`
- Active implementation plans: `docs/superpowers/plans/`
- Provider decisions: `docs/decisions/`
- Sanitized n8n exports: `workflows/n8n/`

## Commands

- Full verification: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`
- Python tests: set `STOCK_PYTHON` to Python 3.12+ when `python` is not on PATH.
- Node tests: `npm.cmd test`
- Workflow audit: `npm.cmd run audit:workflows`

## Safety Rules

- Research only. Never create, modify, cancel, or simulate a real brokerage order.
- Treat model output as commentary. Prices, indicators, filters, scores, and backtests are deterministic code.
- Use KRX Open API as the first official daily OHLCV provider and DART for filings.
- Keep source publication time and observation time. Reject future-dated evidence in point-in-time runs.
- Do not commit credentials, account identifiers, tokens, passwords, production holdings, or unsanitized workflow exports.
- Keep committed n8n workflows inactive and sanitized.
- Do not write to Telegram, WIKI, n8n, or production databases from tests.
- Use `확인 불가` when evidence is missing or conflicting.
- Production deployment, credential registration, WIKI writes, Telegram cutover, n8n activation, production DB changes, Slack sends, and external pushes require explicit approval.
- Do not claim completion without fresh output from the full verification command.

