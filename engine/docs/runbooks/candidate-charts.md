# Final-candidate technical charts

2026-10-20 request: attach charts only to final recommendation/watch candidates. Do not chart the financial-pass research queue automatically.

The opt-in host postprocessor renders up to the report's existing candidate limit. Each PNG shows up to 120 stored daily OHLC candles, SMA20/SMA60, volume, the existing volume ratio and ATR14, the price session, report as-of time and report ID. It does not infer support, resistance, entry prices or new risk rules. SMA lines start only after their required history is available.

The chart is bound to the exact report ID, `price_input_fingerprint` and saved technical metrics. Source and research SQLite connections are read-only. Future, incomplete, mismatched or short price histories produce a visible attachment-unavailable reason. A chart failure preserves the text report and stored decision. No candidate means no automatic chart. No report, score, rank, ledger or order is changed.

Each attachment also includes the actual method names, purpose, calculation basis, measured values, readout and limits. The JSON `technical_analysis` is deterministic commentary with no new trade decision:

| Method | Basis and readout |
| --- | --- |
| Daily OHLC candles | Actual open/high/low/close and close versus open. No inferred reversal or named candlestick pattern from one bar. |
| SMA20/SMA60 | Arithmetic means of the last 20/60 trading-session closes; compare close with each average and SMA20 with SMA60. Above, below and equal remain distinct. Current ordering does not prove a crossover event. [SMA reference](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/sma). |
| Relative volume | Current volume divided by the preceding 20-session average, excluding the current bar. A zero baseline is unavailable, even though the existing engine preserves a zero sentinel. The chart displays N/A and the text explains the missing denominator. No buyer/seller identity is inferred. |
| Wilder ATR14 | The existing engine's Wilder-smoothed true ranges using its latest 61-bar initialization window; state KRW and percentage of the same close. It measures volatility, not direction, and does not establish a new stop or target. [ATR reference](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr). |

An existing `BOTTOM_REBOUND`, `MOMENTUM_CONTINUATION` or `WAIT_FOR_SETUP` classification is shown by name only when stored in the report. Missing classifications are not reconstructed or invented. Actual current price, theme-cycle confirmation and entry conditions remain separate from this saved-data readout. The Telegram text explains the methods in Korean even when the image uses English font fallback.

## Runtime

`stock_assistant.candidate_charts` runs on the **host's existing Hermes Python**, where Pillow is already installed. The dependency-free engine container remains unchanged. The optional packaging extra `charts` declares Pillow for other installations. Pillow's [ImageDraw](https://pillow.readthedocs.io/en/stable/reference/ImageDraw.html) and [ImageFont](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html) APIs are the rendering reference.

The installed Hermes cron delivery calls `BasePlatformAdapter.extract_media` then filters media paths. The postprocessor emits `MEDIA:/absolute/path.png` alongside the original candidate report. The default output directory is CIO's private `cache/candidate-charts/<report_id>/` and files use mode 0600. Output never contains account identifiers or credentials. No new outbound sender is installed.

Korean labels use an existing Malgun/Noto CJK/Nanum font if available, or `STOCK_CHART_FONT_FILE` may select an existing font. Without a CJK font the image uses standard English labels and the six-digit code; the Telegram text still contains the Korean company name and status. The preview uses synthetic data and is explicitly marked as a demonstration, not a real recommendation.

## Activation

Back up and compare hashes before installing these two active files:

- `src/stock_assistant/candidate_charts.py` under the current engine release's host source directory.
- `deploy/jobs/run_stock_cycle.sh` under the same current release.

Preserve unrelated existing changes, credentials, schedules and model settings. Verify the host Python's existing Pillow and engine imports first. In the existing schedule environment, enable `STOCK_CANDIDATE_CHARTS=true` and, if required, set:

```text
STOCK_CHART_PYTHON=/home/ubuntu/.hermes/hermes-agent/venv/bin/python
STOCK_CHART_OUTPUT_DIR=/home/ubuntu/.hermes/profiles/stock-cio/cache/candidate-charts
```

The job continues to use its existing source-state host directory. A custom deployment can set `STOCK_STATE_HOST_DIR`. No container rebuild, engine restart, cron schedule change, Telegram token change or extra stock-service endpoint is required. Roll back by restoring the prior two files and prior environment bytes; setting the flag to false disables attachments without affecting the text report. Do not generate a new recommendation or send a Telegram message merely to test activation.

## Verification boundary

Offline fixture checks cover final-only selection, exact report binding, unchanged databases/report, SMA warm-up and numeric consistency, missing and invalid price inputs, zero-volume/flat-price rendering, safe media captions and PNG decoding. The shell integration replaces Docker and the renderer with local fakes to check enabled/disabled attachments, renderer failure and daily-refresh failure. These tests do not send Telegram messages or access production databases.

Operational activation and actual final-candidate Telegram photo delivery require separately observed evidence. A synthetic preview or successful media-path extraction alone does not prove delivery.

### 2026-10-20 observed validation

| Check | Result | Evidence |
| --- | --- | --- |
| Final-only selection, explanations and failure paths | PASS | 18 chart tests and 4 mocked shell-cycle tests. |
| Fresh full offline regression | PASS | Engine 453 tests, dashboard 93 tests with 2 skips, Node 27 tests, authentication-input 4 cases, fixture validation and `git diff --check`. The first restricted-environment run stalled and was interrupted; the complete rerun in the normal execution environment passed. |
| Preview | PASS | Actual 1600×1120 PNG rendered and visually inspected; OHLC candles, both moving averages, volume, source/report metadata and synthetic-data notice are legible. |
| Deployed server runtime | PASS | The installed `2026-10-20.2` renderer generated a 1600×1120 synthetic PNG with Pillow 12.3.0 and four method explanations. Native Hermes extraction found one image and its media policy accepted it. A mocked Docker/renderer pipeline exercised the deployed shell script on Linux. No Telegram send or production DB write. |
| Exact saved report CLI | PASS | Both deployed CLI formats read `UR-c6ebb470282f5553ee660810` as-of 2026-10-19 11:01:47 UTC. Zero final recommendation/watch candidates produced zero chart items and empty Telegram attachment text. No pending-research stock was promoted. |
| Production activation | PASS | Installed and enabled at 2026-10-20 11:25:28 KST after explicit user approval. Host hashes, environment readability, unchanged cron/wrappers/CIO configuration, active CIO gateway and engine health were checked. `orders_enabled=false`. |
| Real final-candidate photo delivery | UNOBSERVED | No eligible candidate existed at verification. The next scheduled run and Telegram receipt of an actual candidate image/explanation have not been observed. No extra schedule or test send was created. |

The explanation regression cases cover the exact indicator inventory, above/below/equal boundaries, undefined zero-baseline volume, volume denominator and ATR units, stored-only setup labels and safe handling of older attachment metadata. The synthetic preview was regenerated and visually checked with the method names on the image and a matching Korean readout.

### Deployment and rollback evidence

The existing release remains `/srv/stock-assistant/releases/20261014T070049Z-unified`. Only the new `src/stock_assistant/candidate_charts.py`, the chart additions to `deploy/jobs/run_stock_cycle.sh`, and three appended chart settings in `/srv/stock-assistant/schedule.env` were installed. The server shell baseline matched the local file except for those chart additions. The dependency-free engine container was not rebuilt and no service was restarted.

Enabled settings are `STOCK_CANDIDATE_CHARTS=true`, the existing Hermes Python path above, and CIO's `cache/candidate-charts` output directory. Existing morning/evening/weekly wrappers source this environment for each run; all schedules remain unchanged. Intraday alerts remain disabled.

The private backup is `/home/ubuntu/.hermes/backups/candidate-charts/20261020T022527828174Z`. It contains exact prior file/environment bytes, mode/owner metadata, SHA-256 before/after hashes and a guarded rollback script. To restore, first review that manifest, then run:

```sh
sudo -n python3 /home/ubuntu/.hermes/backups/candidate-charts/20261020T022527828174Z/rollback.py
```

Rollback refuses a changed current release or files whose deployed hashes have drifted. It restores the previous shell/environment and removes only the newly installed chart module. No rollback was executed during validation.

Private local evidence is under `.runtime/candidate-charts/`: `verification-deploy.txt`, `deploy-before.json`, `deployed.json`, `preflight-result.json` and `postcheck-result.json`. These files are ignored, contain no credentials and are not publication artifacts.
