# Oracle daily paper service

Run the repository verification before explicitly deploying:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -Python <Python312-or-newer>
py -3.13 -B scripts/oracle_daytrade_deploy.py --deploy
```

The installer owns `/srv/daytrade` and the three `daytrade-*` user units only.
It archives a SQLite backup of the approved 2026-11-16 closed ledger and creates
an immutable release with a SHA-256 manifest. Existing releases are retained.
No existing stock containers or Hermes gateway are restarted.

`daytrade-paper.timer` starts at 08:55 Asia/Seoul on weekdays. The paper service
also starts at user-manager boot (linger required) to recover a reboot during
market hours. Calendar validation skips holidays and nonstandard sessions.
SIGTERM before market close preserves OBSERVING; close at 15:30 records CLOSED
or UNFLATTENED. An unresolved prior day blocks a new cash allocation.

Observe with `systemctl --user status daytrade-paper.service daytrade-paper.timer
daytrade-dashboard.service`, `/srv/daytrade/state/status.json`, and
`journalctl --user -u daytrade-paper.service`. `exit.json` contains safe error
classes and source locations, never secret-bearing exception text.

The read-only dashboard binds to 127.0.0.1:8765. Forward with:

```text
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <existing-Oracle-SSH-host>
```

Then open `http://127.0.0.1:8765/daytrade`. The dashboard resolves state/latest
for every report, opens the actual daily database read-only and rejects POST.

Shared tokens are read only; the existing morning job owns issuance. Expiry or
revocation never triggers OAuth. Telegram uses the existing stock-cio bot and
its uniquely validated home session. Failed/uncertain delivery reservations
require manual reconciliation and are never retried automatically.

Rolling completed candles survive restarts with the strategy's full 200-bar
window. Partial subscription/reconnect minutes are omitted. Observed zero-trade
bars are not certified tape completeness; the provider supplies no sequence.

To revert code, stop only the two daytrade services, atomically repoint `current`
to a retained compatible release, restore its unit files from
`state/deployment-backups/<release-id>`, reload the user manager and start them.
Never replace daily ledgers or the shared token cache during code rollback.
