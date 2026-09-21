# Hermes Investment Profiles

These files are templates for four independent official Hermes profiles. They are not installed by this repository.

Create the profiles only after the Oracle Hermes installation and authentication method are approved:

```bash
hermes profile create stock-cio --description "투자 리서치 총괄, 승인과 최종 보고"
hermes profile create stock-market --description "국내 시장, 가격, 거래량 분석"
hermes profile create stock-fundamentals --description "재무, 공시, 계약과 실적 분석"
hermes profile create stock-risk --description "반대논증, 희석, 경영진과 손실 위험 검토"
```

Copy only the matching `SOUL.md` into each profile. Set every profile's `terminal.cwd` to the deployed stock-assistant directory. Do not copy provider OAuth credentials between profiles; official Hermes documentation requires each profile to own its credentials.

The profiles may use the read-only stock API or CLI. They must not receive brokerage order tools or a general WIKI writer. The CIO alone may request a journal preview; a separate approval service must consume the one-time approval before any WIKI write.

