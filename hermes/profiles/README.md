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
