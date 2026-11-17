# Oracle daytrade service implementation plan

Goal: deploy the approved daily paper collector and read-only dashboard to /srv/daytrade.
Spec: C:/Users/USER/.gemini/antigravity-cli/brain/d449c3ba-6616-40ac-8eb1-55ce186164fb/scratch/daytrade-oracle-service-deploy-spec.md

Architecture: reuse LivePaperSession, LiveCandleBuilder, LiveFrameBuffer, RestAdapter,
WebSocketAdapter and notify_new_fills. Keep the existing audited ledger and strategy unchanged.
Use per-day real directories and resolve latest at read time (ledger rejects file symlinks).

Constraints: paper only; shared token read only; no candles REST; no unrelated service changes.
Work in codex/daytrade-oracle-service-deploy preserving all existing dirty files. No Git publication.

- [x] Task 1: add failing daemon tests for calendar gates, preopen wait, daily cash carry,
  restart sequence/metadata/candles, injected fill sender, stop and close. Implement daemon.py
  with open_or_resume_session(state_root, day), run_trading_day and injected clocks/transports.
- [x] Task 2: add deployment tests; implement scripts/oracle_daytrade_deploy.py and
  engine/deploy/daytrade units, isolated read-only dashboard, immutable release staging,
  SQLite backup import, service env, start and rollback metadata.
- [x] Task 3: run scripts/verify.ps1 using Python313/python.exe. Deploy only after success.
  Observe >=4 completed 1m bars, services, calendar, cash carry, names, indicators,
  single socket, HTTP success, token bytes and protected service identities unchanged.
- [x] Task 4: save sanitized result.json and notify live-verified w6:p2 per spec.

Review focus: crash between checkpoints; reboot monotonic reset; unflattened prior day;
partial minute after reconnect; token expiry and failed Telegram send.
Restart restores runner from audited events and meta, never replays fills. Unflattened latest
day blocks new capital instead of silently abandoning positions. Gaps must not synthesize bars.
SIGTERM during market hours checkpoints OBSERVING for a subsequent restart.

Independent read-only review found three restart defects. Fixed with regression tests:
normalized/provider candle shapes; exact REST-lock recovery with persistent backoff;
200-bar strategy history preservation (35-bar equality before/after restart).
Final offline verification passed: dashboard 131 (2 existing skips), engine 617
(1 existing optional Pillow skip), Node 27, credential-input checks 4.
Daemon lifecycle tests: 14, no skips or failures. Added scope/runtime tests: 4;
deployment/dashboard tests: 3. Protected source files remain unchanged.

2026-11-17 09:40 KST live acceptance: all three units active, zero restarts,
8 symbols x 4 completed 1m bars, computed VWAP/volume ratios, single WS,
initial capital 9995188.8384, page/report HTTP200, protected services/auth unchanged.
Local SSH forwarding was rejected by automatic approval review; manual access
instructions are in result.json. No live fill occurred in the observation window,
so actual Telegram delivery and the future15:30 close remain unverified.
