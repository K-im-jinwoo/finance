# Stock Investment Assistant Guide

## Start Here

- Architecture: `ARCHITECTURE.md`
- Product plan: `docs/superpowers/plans/2026-09-21-hermes-stock-investment-assistant.md`
- Phase 0 findings: `docs/findings/2026-09-21-phase-0.md`
- Phase 1 execution plan: `docs/superpowers/plans/2026-09-21-phase-1-data-contracts.md`

## Commands

- Full verification: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1`
- Python tests: set `STOCK_PYTHON` to Python 3.12+ if `python` is not on PATH, then run the verification script.

## Safety Rules

- Research only. Never create, modify, cancel, or simulate a real brokerage order.
- Treat model output as commentary. Prices, indicators, filters, scores, and backtests are deterministic code.
- Do not commit credentials, account identifiers, tokens, or production holdings.
- Keep source publication time and observation time. Reject future-dated evidence in point-in-time runs.
- Do not write to Telegram, WIKI, n8n, or production databases from tests.
- Use `확인 불가` when evidence is missing or conflicting.
- Production deployment, credential registration, WIKI writes, Telegram cutover, and external pushes require explicit approval.

