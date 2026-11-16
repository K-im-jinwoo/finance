# Daytrade Stage 1 offline tools

Mode is always `OBSERVE_ONLY`. No score weights, actual fee schedule, stop-loss
parameters, or Stage 2 policy are selected by this package. There is no path to
`PAPER_READY` or a real brokerage order. The existing `intraday.py`, research
pipeline and paper ledger are separate.

## Run a synthetic replay

From the finance repository root, select Python 3.12+ and an **existing new scratch
directory**. A ledger is created only at `<scratch>/.runtime/intraday-paper.sqlite3`.
The command refuses to overwrite an existing file. Keep the scratch directory
under the repository's ignored `.runtime/` or outside Git.

```powershell
$env:PYTHONPATH = 'engine/src'
& <Python 3.12+> -B -m stock_assistant.daytrade replay engine/tests/fixtures/daytrade_synthetic.json --root <scratch>
```

The fixture deliberately uses arithmetic stress fee/tax assumptions. They are
not real account rates or an approved trading policy. Only datasets explicitly
marked `SYNTHETIC` can execute fixture intents/fills. `OBSERVATION` datasets record
observations and unscored signals, and refuse execution. The CLI performs no
network I/O, credential reads or remote actions. Invalid input prints a safe
error class and retains prior data, including any valid prefix already applied.
Use a new scratch root after an invalid first-run dataset; never delete an old
ledger to rerun it automatically.

Output includes `ledger_hash`, `dataset_hash`, `report_hash`, seed, event/fill
counts and an independent cash audit. Two new roots with the same input produce
the same report. `ledger_hash` excludes filesystem paths and wall-clock run time.
`dataset_hash` preserves input bytes' semantic JSON content, including array
order. Explicit event `sequence` disambiguates equal timestamps. Conflicting
sequence/identity, time reversal, future data, changed seed or policy are errors.

To resume without reinitializing, open `DaytradeLedger(root)` and call
`ReplayEngine(ledger, seed=<original seed>).run(events)`. Already-applied inputs
are checked before consuming random state or mutating the DB. The stored seed,
policy and costs are immutable through the public replay interface.

## Components and limits

- `http.py`: injected public-only REST transport, no default real network client.
  Every fetch makes at most one attempt; the caller honors `retry_at`. Group
  cooldowns are independent. Positive Remaining does not override HTTP 429.
  Repeated 429 latches degradation; only exit-monitor reads are admitted after
  cooldown until successful observation and verified recovery for that group.
  Diagnostics expose only bounded API code and request ID, never message/data.
- `market.py`: immutable-by-copy normalized payloads, explicit quality thresholds,
  completed/end-timestamp minute bars, prior-day KRX daily data, KRW-only values,
  stale/null/empty/crossed books and rankings. Integrated KR and KRX daily series
  have separate source/market labels. `bar_typical_vwap_approx` is an approximation;
  `order_strength` and `tick_vwap` remain `UNAVAILABLE`.
- `websocket.py`: no I/O until an explicit socket factory is invoked. Stdlib
  protocol tests cover full-replace, partial rejection, scope checks, 60s PING,
  pong timeout, local 5-declaration/s limit, close-before-reconnect and revoked
  authentication. The adapter never refreshes a token. A failed close blocks a
  replacement connection. LOSSY feeds cannot certify zero upstream frame loss.
- `risk.py`: session phase and independent reason-scoped locks. A time transition
  cannot clear data/rate/loss locks. Loss is based on daily opening net equity,
  including unrealized changes and fixture liquidation costs, not just cash P&L.
- `ledger.py`: application identity, exclusive creation, atomic event/state/fill
  writes, idempotency, independent cash/quantity audit. Synthetic fills use the
  latest post-intent post-latency book, explicit participation and adverse
  slippage, no duplicate spread charge. A quote snapshot can be consumed only
  once. Position limits are assessed at entry using current marked equity and
  cost-inclusive additional exposure; price drift does not auto-rebalance.
- `replay.py`: deterministic offline integration including seeded synthetic HTTP
  responses and persisted per-group backoff state across resume. No hidden random
  or wall-clock inputs affect accounting.

Time defaults on explicitly verified normal sessions: entry cutoff 14:50 KST,
exit requests 15:15, close 15:30, per-name 20%, up to 3 names/60% total, daily loss
trigger -3%. These are control triggers, not guarantees of fills or maximum loss.
An exit request does not manufacture a fill. Missing/stale books cause
`MARK_UNCERTAIN`; unclosed positions become `UNFLATTENED` and block subsequent
entries. Normal calendars and real thresholds still require verified inputs.

`websockets>=15,<18` is an optional `daytrade` extra. Runtime compatibility and live
subscription behavior are unverified. The offline suite does not install/import
it, uses no live server, and has no websocket-related skips. A future live socket
factory requires confirmed account slot availability and the existing shared
token supplier; supplying that flag is not authorized by this implementation.

## Verification

```powershell
$env:PYTHONPATH = 'engine/src'
& <Python 3.12+> -B -m unittest discover -s engine/tests -p 'test_daytrade_*.py' -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -Python <Python 3.12+>
```
