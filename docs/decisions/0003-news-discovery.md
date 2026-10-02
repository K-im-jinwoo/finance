# Decision 0003: News discovery as a separate research queue

Date: 2026-10-02 KST

Status: Implemented locally; production activation and credential connection require approval.

## Behavior

Use the [official NAVER API Hub news search API](https://api.ncloud-docs.com/docs/naver-api-hub-search-news).
The URL is `https://naverapihub.apigw.ntruss.com/search/v1/news`, with JSON format and the
`X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY` headers. Credentials must be from an API Hub
Application with NAVER search / news enabled. General Ncloud account access keys are not this API's
Application credentials. The [official migration notice](https://developers.naver.com/notice/article/32530)
states that new search API applications moved to API Hub on 2026-07-31. Legacy developer-center
documentation remains online for older users; it is not the registration or endpoint for this new feature.
Search five broad queries (`특징주`, `공급계약`, `흑자전환`, `허가 승인`, `정책 수혜`),
with date sorting, one page of at most 100 results per query and a 10-second request timeout.
The default article window is 48 hours; CLI allows 1 to 72 hours. This is a bounded search sample,
not complete coverage of every listed stock, every news item or every theme.

Map explicit issuer names in headlines to the stored, currently listed common-stock universe.
Do not infer unnamed theme beneficiaries, fuzzy company aliases, subsidiaries or peers.
Ambiguous multi-issuer headlines and short generic names without an issuer-subject marker are skipped.
Detect reported contracts, earnings improvements, approvals and explicit policy selection/benefits.
Expectation-only, rumor, marketing/photo and negative earnings headlines do not become positive leads.
Cancellation, denial and contract termination reports suppress older positive news for that issuer.
Issuer names absent from the stored universe cannot be resolved automatically.

Preserve canonical original URLs, cleaned headlines, provider `pubDate`, actual response observation
time and first collection time. Naver describes `pubDate` as time supplied to Naver or the original
source-provided time; it is not certified original publication time. Reject future or unobserved
evidence and expire by that provider timestamp, never by refresh time. Deduplicate overlapping
queries, canonical URLs and identical cleaned headlines. Store only a bounded search description,
not article bodies. Articles are untrusted data, never instructions or confirmed DART facts.

News leads enter DART enrichment in addition to the existing top 30, at most ten extra issuer codes.
Reports show up to five leads under `뉴스 발견 — 추가 검토`, with NEW/UNCHANGED, article evidence,
existing screening result, actual daily-price date and official/price verification requirements.
Missing financials do not prevent a lead from entering the queue; existing rule exclusions remain
excluded. News alone cannot alter deterministic scores, the existing top-five list or paper-account
entry policy. Contract 1.3 adds `news_discovery` only when requested; default 1.2 payloads and IDs remain
unchanged. `GET` of a stored report returns the same saved evidence without a new external fetch.

## Execution

Credentials are read from files, never command arguments or logs:

```sh
python -m stock_assistant discover-news --database /var/lib/stock/stock-assistant.sqlite3 \
  --client-id-file /run/secrets/naver-news-client-id \
  --client-secret-file /run/secrets/naver-news-client-secret
python -m stock_assistant generate-candidates --database /var/lib/stock/stock-assistant.sqlite3 \
  --include-news --format text
```

`enrich-dart` and `enrich-dart-disclosures` accept `--include-news`.
The API accepts a boolean `include_news` on `POST /v1/repository/candidates`; the client uses
`generate_candidates(as_of=..., include_news=True)`. Neither route fetches news or takes news credentials.
Two additive tables, `news_discovery_runs` and `news_discovery_events`, are initialized only by the
opt-in collector; reading an old DB does not migrate its schema. Production DB changes remain gated.

## Scheduled rollout

Default scheduled behavior stays disabled. After approval:

1. Supply Naver search-enabled credential files through the approved secret registration process.
2. Set `NAVER_NEWS_CLIENT_ID_HOST_FILE` and `NAVER_NEWS_CLIENT_SECRET_HOST_FILE` in candidate.env.
3. Load `deploy/compose.news.yaml` alongside the base compose and update the deployed source/profile.
4. Export `STOCK_NEWS_ENABLED=true` in the host schedule.env used by the existing wrappers.
   Set the approved `STOCK_DART_BUSINESS_YEAR` there as before. candidate.env alone does not activate
   the shell flag; do not add credentials as environment values.
5. Confirm actual API timestamps, issuer matching, private health, orders disabled, saved report,
   and one approved Telegram rendering before relying on the next scheduled run.

No new cron or increased intraday polling is required. Morning, evening and weekly jobs collect
news before due diligence. Enabled successful morning collection also performs financial enrichment.
Each query failure fails the batch without retrying; it is saved as UNAVAILABLE rather than zero news.
The job still emits the existing research report and explicitly marks news unavailable. The
`--news-fetch-failed` report flag also prevents reuse if the collector cannot save its failure state.
An otherwise successful cache becomes unavailable after 12 hours without a new successful collection.

Rollback the feature by exporting `STOCK_NEWS_ENABLED=false` and reverting the opt-in compose/profile
configuration through the approved deployment procedure. Preserve existing reports and news tables;
do not delete operational data. The separate KRX daily-data freshness incident remains unresolved by
this feature and still limits price-based judgments.

## Verification boundaries

Network-free fixtures cover receipt times, duplicate/stale/future news, exact/ambiguous issuer mapping,
material categories, cancellations, provider failure, first observation, expiry, low-score lead
enrichment, unchanged top-five results, exclusion preservation, API/client opt-in and stored rendering.
Shell fixtures execute the real job using fake Docker and no external services. These checks do not
prove real API credentials, article coverage, production DB operation or Telegram delivery.
