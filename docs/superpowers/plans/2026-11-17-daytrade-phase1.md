# Daytrade Phase 1 implementation plan

Approved spec: C:/Users/USER/.gemini/antigravity-cli/brain/814b212b-e385-49cf-81ea-11716a4797a4/scratch/phase1_contract.md
Execution: inline, test first, independent final review; remain on the specified branch.

Goal: apply approved experimental common-stock screening and defensive entry controls
to today's running paper ledger, preserving fills, capital, positions and 200-bar history.

- [x] Add failing tests for optional scanner/strategy settings, metadata round-trip,
  cache expiry/failure, bounded queue, protected rotation, entry reservations/cooldowns,
  weak-exit grace and same-day settings migration.
- [x] Implement compatible filters in scanner.py/stream.py; isolated stock metadata
  cache in metadata.py; strategy settings and ledger-derived entry history in bot.py.
- [x] Integrate nonblocking pre-screen metadata and protected rotation in daemon.py.
  Live settings: price<=200000; cap50000000000..10000000000000; bid notional20000000;
  entries5; cooldown1200s; entry09:05; weak exit grace120s. MetadataTTL300s,
  rotation minimum120s and maximum2 replacements/cycle. Keep original REST paths.
- [x] Independent read-only review, regression fixes, full scripts/verify.ps1 exit0.
- [x] Back up today's live ledger during controlled service stop, deploy immutable
  release using existing installer, verify active units/protected invariants and
  actual selected common stocks/status.json. Save .runtime/daytrade-phase1/result.json.
- [x] Report changed files and fresh evidence to live-verified w6:p3.

Preserve POLICY_VERSION, approved costs, real orders disabled, -3% loss lock,
14:50 entry cutoff/15:15 exits/15:30 close, original metadata when new options disabled.
No /stocks/all, warnings, sectors or candles endpoint expansion. Preserve unrelated
dirty changes and the separate research ledger. No commit/push requested.

Review focus: stale metadata must expire candidate validity; held names survive filters;
failed cache lookups retry with delay; callbacks never block on a full work queue;
partial fill counts once and reservations prevent entry-limit overshoot; restarts
merge live settings without replaying intents/fills; subgroup warm-up gaps never fabricate bars.

Verified: new22 regressions pass(no skips), full131 dashboard/639 engine with existing
2/1 optional skips, Node27/auth4; independent review findings corrected. Release
20261117T020647Z deployed. At11:10KST all3 units active, NRestarts0, eight eligible
common stocks, HTTP200 and protected/auth invariants;10469 prior events and2 fills
preserved. Intraday settings migration and actual nonheld slot rotation observed.
Pre-existing ConnectionClosedError/restart23 addressed with network exception and
graceful-close regressions; observation does not establish long-run stability.
