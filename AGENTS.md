# Finance repository guide

- Finance is the canonical repository for the stock engine, stock Hermes profiles, dashboard and local paper ledger.
- Dashboard: `src/research_dashboard`, `web`, `scripts`, `tests`.
- Engine: `engine/src/stock_assistant`; follow `engine/AGENTS.md` for engine changes.
- `vendor/stock-assistant` and `docs/reference` are archived validation evidence. Do not update them as active source.
- Full verification: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -Python <Python 3.12+ executable>`.
- Use `codex/<task>` branches. Commit explicit files; inspect changes and verification before merging into `main`.
- The initial engine import preserves original commit IDs. Do not squash its integration merge or rewrite existing history.
- Preserve original stock worktrees and their changes. Do not reset, clean, remove or archive them without explicit approval.
- Keep credentials, account data, databases, runtime state, local backup bundles and logs outside Git.
- Tests are offline and use fixtures or mocks. Do not send Telegram messages or write production data from tests.
- Preserve the approved paper-account policy. Never create, modify or cancel real brokerage orders.
- GitHub changes do not deploy to the server. Production deployment requires separately confirmed approval.
