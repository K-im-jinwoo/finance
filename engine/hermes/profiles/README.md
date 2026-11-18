# Hermes Investment Profiles

These files are templates for five independent official Hermes profiles. They are not installed by this repository.

Create the profiles only after the Oracle Hermes installation and authentication method are approved:

```bash
hermes profile create stock-cio --description "투자 리서치 총괄, 승인과 최종 보고"
hermes profile create stock-market --description "국내 시장, 가격, 거래량 분석"
hermes profile create stock-live-market --description "Toss 읽기 전용 현재가와 장중 거래량 관측"
hermes profile create stock-fundamentals --description "재무, 공시, 계약과 실적 분석"
hermes profile create stock-risk --description "반대논증, 희석, 경영진과 손실 위험 검토"
```

Copy only the matching `SOUL.md` into each profile. Set every profile's `terminal.cwd` to the deployed stock-assistant directory. Do not copy provider OAuth credentials between profiles; official Hermes documentation requires each profile to own its credentials. Copy `stock-live-market/scripts/stock-intraday.sh` only to that profile and `stock-cio/scripts/stock-live-market-query.sh` only to the CIO profile, then make both executable.

Copy the common `hermes/skills/stock-research` directory into the CIO, market, fundamentals, and risk profiles. Install the matching `tool-policy.json` beside every profile configuration as a review artifact. The live-market profile does not receive a stock API role token; it can execute only the bounded read-only intraday wrapper. The policy files document intent; the stock API and wrapper enforce the data boundaries. Give an API profile only its own token file, never the server's complete role-secret JSON.

Only the CIO receives Telegram. `stock-live-market` is an internal delegated profile, not another user-facing Telegram bot. Canonical Desktop Bot Chats use `message_agent`; Telegram sessions use the bounded synchronous CIO bridge because official Hermes does not expose `message_agent` outside canonical Bot Chats. Disable Telegram interim assistant and long-running notifications so one request produces only the final overlay or one failure response. The profiles must not receive brokerage order tools or a general WIKI writer. The CIO alone may request a journal preview; a separate approval service must consume the one-time approval before any WIKI write.

For Telegram specialist reviews, install `stock-cio/scripts/stock-expert-review.py` under the CIO profile's `scripts/` directory and the matching SOUL guidance in the CIO, market, fundamentals and risk profiles. Install both `stock-research/SKILL.md` and the nested `stock-research/investment-committee-review/SKILL.md` in CIO's skills directory: Telegram can select the nested committee skill directly. Register it only in CIO's `mcp_servers.stock_experts` with command `python3`, arguments `["/home/ubuntu/.hermes/profiles/stock-cio/scripts/stock-expert-review.py", "--mcp"]`, timeout 100 and connect_timeout 10. CIO calls the one structured `stock_expert_review` tool; it does not generate shell commands or weaken terminal approval settings. A stored-review request with an exact ID goes directly to the bridge, which performs the report lookup; CIO does not first repeat exact-ID HTTP or client calls. The server starts the three real profiles once each in parallel, in fresh `--source tool` conversations with `--toolsets none`; it does not resume canonical Bot Chats, send Telegram messages or copy credentials. The CLI resolves each profile's own existing model/authentication. CIO provides the relevant original evidence and synthesizes completed memos, disagreements and missing work into one response. An unavailable specialist remains explicitly unavailable. A pure saved-report display does not start a meeting.

The bridge accepts bounded JSON on stdin or `--request <private-file>`. Stored reviews read the exact report ID; latest GET is used only when its ID matches. Long lineage and pending lists are excerpted with exact omission counts and pending-reason totals. A historical-ID API failure is reported, never replaced with another report. Prospective trades follow `STAGED_TRADE_REVIEW_V1`: preserve the two existing screening definitions, investigate news/company evidence, verify theme-cycle evidence and then evaluate technical entry/invalidation/exit conditions. Missing evidence does not become an invented price or completed stage. Engine thresholds, rankings, scheduled jobs and the old paper contract remain separate.
