# Daytrade urgent hotfix plan

Spec: C:/Users/USER/.gemini/antigravity-cli/brain/814b212b-e385-49cf-81ea-11716a4797a4/scratch/hotfix_contract.md
Approved: implementation, offline/full verification, Oracle rollout and live observation.
Continue current codex/daytrade-oracle-service-deploy; preserve unrelated changes.

- [ ] Add failing tests for bounded live summaries/latest signal index, resume
  trimming, offline full history, immediate INTENT/scan/fill checkpoints,
  512-frame/50ms bounded receive, stale ranking watch protection and safe recovery.
- [ ] Implement recent20 scans/200 signals, bounded diagnostic details and
  single-serialization snapshots; preserve all original ledger events/fills.
  Keep latest per-symbol signals and rebuild last_signal after trimming.
  Limit curve diagnostics transparently so truncated history cannot imply full MDD.
- [ ] Merge live ranking age90s into quality+scanner (quote age5 unchanged),
  bid notional5000000, live checkpoint4s and bounded burst512/50ms.
  Keep subscriptions on stale source snapshots while blocking entries; do not
  retain symbols that fail additional explicit safety filters.
- [ ] Independent review; full scripts/verify.ps1 exit0; existing deployment
  script with today's SQLite snapshot; inspect protected/auth/event preservation.
- [ ] Observe at least90s AND complete new-pool warm-up/strategy evaluation;
  summary<500KB, WS LIVE, no DATA_DEGRADED, no stale/warmup trap; report to w6:p3.

No order endpoints, new REST paths, fee-policy changes, unrelated source edits,
Git commit/push, or fabricated missing candles. Prior diagnostic full cache is
preserved in the installer's pre-rollout SQLite backup. No memory edits.
