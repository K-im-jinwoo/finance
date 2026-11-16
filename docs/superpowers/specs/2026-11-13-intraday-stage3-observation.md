# Project 1: 당일 단타 Stage 1 — 3단계 실서버 관측 설계 및 수수료·세금 후보

기준: 2026-11-13 KST. 작성: Gemini(AGY). 실행·구현: Codex.
연계 설계: [Stage 1 설계](2026-11-13-intraday-stage1.md).

본 문서는 오프라인 기반 구현(`daytrade/`, 테스트 55건 `skip=0`) 이후 **3단계(`OBSERVE_ONLY` 실서버 관측)** 의 설계, 수용 기준, 4단계 전환 전 공식 자료 기반 **수수료·세금 후보(미확정)**, 그리고 Codex 전달용 지시문을 정의한다.

---

## 1. 3단계 관측 범위와 안전 제약

1. **A (WebSocket 관측)**: **계정 전체 빈 슬롯 확인 + 사용자 실행 승인**이 내려진 경우에만 실행한다.
   - 토스 AsyncAPI 1.2.3([asyncapi.json](../../.runtime/intraday-spike/asyncapi.json)) 기준 계정당 동시 WebSocket 연결 한도는 2개이며 초과 시 가장 오래된 연결이 종료된다.
   - 빈 슬롯 확인 전에는 `--websocket-slot-available` 실행 및 실서버 연결을 금지한다(`NOT_RUN_ACCOUNT_SLOT_UNKNOWN` 유지).
2. **B (`/api/v1/candles` 429 재확인)**: **일단 보류**한다.
   - 2026-11-13 13:35 KST 실측에서 `X-RateLimit-Remaining: 19` 상태의 HTTP 429가 관측되었으나 원인이 미확인 상태다. 별도 안전 확인 결정 전까지 분봉 연속 재호출을 금지한다.
   - 참고: `toss_intraday_probe.py`에는 `api_error_code` / `request_id` 안전 추출이 이미 반영되었으며, 향후 B 재개 시 `candles` 호출 여부를 분리 플래그(`--skip-candles` 등)로 통제할 수 있어야 한다.
3. **불변 경계**:
   - 기존 `SharedTokenCache` 읽기 전용 재사용 (OAuth 발급/캐시 무효화/파일 쓰기 금지, 충분한 잔여 TTL 확인).
   - `orders_enabled=false`, `OBSERVE_ONLY` 유지, `.runtime/hermes-paper.sqlite3`·운영 DB·Telegram·실주문 미접촉.
   - 1연결 · 소수 종목(`005930,000660`, 최대 5종목) · `trade:kr` + `orderbook:kr`만 구독 (`personal:*` 절대 금지).

---

## 2. 3단계 실서버 관측(A) 설계 및 측정 항목

승인된 1회 관측에서는 기존 REST 표본과 중복되는 `candles` 버스트를 피하고, WebSocket 연결 품질·프로토콜 호환성만 집중 실측한다.

| 검증 항목 | 측정 내용 | 합격(수용) 조건 |
|---|---|---|
| 핸드셰이크·라이브러리 호환성 | 호스트 `websockets` 실제 버전으로 TLS 핸드셰이크 성공 | `handshake_ms` 기록, 예외 없이 연결 |
| 선언·Ack 정합성 | `id` 포함 full-replace 배열 전송 → `subscriptions` ack 수신 | 요청 토픽과 `subscribed` 100% 일치, `rejected=[]` |
| Keepalive (`PING`/`pong`) | 샘플 중 텍스트 `"PING"` 1회 송신 → `{"type":"pong"}` 수신 확인 | `pong_received=True`, `pong_rtt_ms` 기록 |
| 시세 프레임 구조·유실 징후 | `trade:kr` / `orderbook:kr` 필드 목록, `orderbook.timestamp is None` 비율, `event_age_ms` 분포(p50/p95/음수) | `currency == "KRW"` 외 0건, 프레임 스키마 오류 0건 |
| 선종료 후 재연결(1회) | `"[]"` 전송 + 소켓 `close()` 완료 후 1회 재연결 및 재구독 ack 확인 | 이전 소켓 완전 종료 후 재연결 성공, 동시 2연결 초과 유발 없음 |
| 인증·운영 불변 | 관측 전후 공유 캐시 SHA-256 비교, 컨테이너 시작/재시작 메타데이터 유지 | `cache_bytes_unchanged=True`, `token_issued=False`, `token_invalidated=False` |

---

## 3. 오프라인 연속 준비 설계 (승인 대기 중 Codex 선구축 가능 범위)

사용자 빈 슬롯 확인·승인을 기다리는 동안 Codex는 아래 **순수 오프라인 보완·브리지**를 구현하고 테스트할 수 있다(실서버 호출 없음).

1. **프로브 안전 진단 확장 (`scripts/toss_intraday_probe.py`)**:
   - `--skip-candles` (또는 `--ws-only` 플래그): B 보류 정책에 따라 승인된 A 실행 시 `/api/v1/candles` 호출을 건너뛸 수 있는 옵션 추가.
   - `sample_websocket()` 내 `PING` 송신 및 `pong_received` / `pong_rtt_ms` 집계, `null_timestamp_counts` 집계, 선택적 `--verify-reconnect`(1차 소켓 `close()` 완료 후 짧은 2차 연결 ack 1회 확인) 추가.
   - `websockets` 미설치 로컬 테스트에서도 fake socket 주입으로 모두 검증 가능하도록 유지 (`skip=0`).
2. **관측 수집→`MarketEvent` 변환 및 품질 리포트 브리지 (`engine/src/stock_assistant/daytrade/`)**:
   - `WebSocketAdapter` 수신 `trade:kr` / `orderbook:kr` 프레임과 REST `rankings` 스냅샷을 `daytrade.market.normalize()`용 `MarketEvent` 입력 형식으로 변환하는 순수 어댑터 함수 추가.
   - 일일 `OBSERVE_ONLY` 관측 세션을 요약하는 오프라인 품질 리포트 함수(종목별 수신 건수, `SOURCE_TIMESTAMP_UNAVAILABLE` 빈도, `STALE_*` / `CROSSED_ORDERBOOK` / `EMPTY_ORDERBOOK` / `RATE_LIMIT_DEGRADED` 발생 횟수, `entry_blocked` 비율) 구현.
   - **주의**: 여전히 점수 가중치, 임계값, `QualityPolicy` 기본값, 비용 일정은 확정하지 않는다.

---

## 4. 수수료·세금 공식 자료 조사 및 후보 (미확정)

> 상태: **후보 조사 단계(미확정)**. 4단계 `PAPER_READY` 판단 시 사용자 계좌 실제 약관(협의/우대수수료·유관기관 제비용 포함 여부) 확인 후 단일 `cost_schedule`을 고정한다.

### 공식 기준 요약 (2026년 적용)
1. **증권거래세 및 농어촌특별세** (기획재정부 세제 개편·증권거래세법 시행령 개정, 2026-01-01 양도분부터 적용):
   - **코스피(KOSPI)**: 증권거래세 **0.05%** + 농어촌특별세 **0.15%** = 매도 합계 **0.20% (`20 bps`)**
   - **코스닥(KOSDAQ)**: 증권거래세 **0.20%** (`20 bps`, 농특세 없음)
   - *(코넥스 0.10% — 초기 단타 대상 제외)*
2. **토스증권 국내주식 기본 매매 수수료** (`tossinvest.com` 안내 기준, 2026-06-30 무료 이벤트 종료 후 표준 요율):
   - **KRX 체결**: **0.015% (`1.5 bps`)** (매수·매도 각)
   - **NXT(대체거래소) 체결**: **0.014% (`1.4 bps`)** (매수·매도 각)
   - **체결 시장 불확실성**: OpenAPI 통합 시세(`INTEGRATED_KR_QUOTE_PAPER`)에서는 KRX·NXT 별도 체결을 보장할 수 없으므로 보수적으로 **KRX 요율(0.015%) 이상** 적용 권장.
   - **미확인 잔여 항목**: 유관기관 제비용(한국거래소·한국예탁결제원 약 `0.0036~0.0042%` ≈ `0.4 bps`)이 위 0.015%에 포함(All-in)되는지 별도 가산되는지, 그리고 OpenAPI 계좌별 협의수수료 여부는 계좌 약관 확인 필요.

### `cost_schedule` 후보 비교표 (단위: `bps`, 1 bps = 0.01%)

| 후보 ID | 매수 수수료 (`buy_fee_bps`) | 매도 수수료 (`sell_fee_bps`) | 매도 세금 (`sell_tax_bps`) | 왕복 명시 비용 | 추가 슬리피지·지연 가정 | 용도·성격 |
|---|---:|---:|---:|---:|---|---|
| **후보 1 (기본 표준안)** | `1.5` | `1.5` | `20` | **23.0 bps (0.23%)** | `0` ~ 별도 측정 | 토스 KRX 기본 수수료(0.015%) + 2026 법정 거래세(0.20%), 유관기관 비용 포함 가정 |
| **후보 2 (보수적 기본안 · Stage 1 권장)** | `2.0` | `2.0` | `20` | **24.0 bps (0.24%)** | 3단계 실측 후 설정 | KRX 1.5 bps + 유관기관 제비용(~0.4~0.5 bps) 별도 가산 대비 |
| **후보 3 (스트레스 민감도안)** | `2.0` | `2.0` | `20` | **24.0 bps** | `extra_slippage_bps = 5 ~ 10`, `latency_ms = 500 ~ 1000` | 4단계 수익성 민감도 검증용 보수적 스트레스 기준 |

---

## 5. Codex 실행 지시문 및 수용 기준 (Stage 3)

### Phase 3-A (지금 즉시 실행 가능 — 순수 오프라인 사전 준비)
- **지시**:
  1. `scripts/toss_intraday_probe.py`에 `--skip-candles`, `PING`/`pong` 측정(`pong_received`, `pong_rtt_ms`), `orderbook.timestamp is None` 건수 집계, `--verify-reconnect`(선종료 후 1회 재연결) 기능을 추가하고 `tests/test_toss_intraday_probe.py`에 오프라인 테스트를 추가하라.
  2. `engine/src/stock_assistant/daytrade/`에 WS `trade:kr`/`orderbook:kr` 및 REST `rankings` 수신 페이로드를 `market.normalize()`용으로 변환하는 브리지와 `OBSERVE_ONLY` 관측 품질 요약 리포트를 구현하고 테스트를 추가하라.
  3. **절대 금지**: `--websocket-slot-available` 실행, 실서버 WS/REST 호출, `QualityPolicy`/점수/비용 임의 확정, `PAPER_READY` 경로 추가, 무관 파일 변경.
- **수용 기준**:
  - `test_toss_intraday_probe.py` 및 `test_daytrade_*.py` (`websockets` import/네트워크 차단) 전부 `skip=0, fail=0` 통과.
  - `scripts/verify.ps1` 종료 코드 0 유지.
  - `--skip-candles` 시 `/api/v1/candles` URL 호출 0건 테스트 확인.
  - `--verify-reconnect` 테스트에서 이전 소켓 `close()`가 2차 `connect()`보다 반드시 선행됨을 검증.

### Phase 3-B (빈 슬롯 확인 + 사용자 명시적 승인 후에만 실행)
- **실행 전 게이트**: 사용자에게서 "다른 PC/프로그램 WS 미사용(빈 슬롯 확인)" 및 "Oracle 1회 읽기 전용 A 실행 승인"을 받은 뒤에만 실행한다. (`candles` 429 재확인은 `--skip-candles`로 제외 유지)
- **수용 기준**:
  - `cache_bytes_unchanged == true`, `token_issued == false`, `token_invalidated == false`, 컨테이너 재시작 0.
  - WS 핸드셰이크 성공, 요청 토픽 100% ack, `pong_received == true`, 선종료 후 재연결 1회 성공, `event_age_ms` 및 `null_timestamp_counts` 결과 JSON을 `.runtime/intraday-spike/`에 저장 후 Gemini(AGY)에 검증 제출.

---

## 6. 이후 연속 진행 계획 (Stage 3 → Stage 4)
1. **Codex Phase 3-A 완료 보고** → Gemini가 오프라인 코드·테스트 독립 검증.
2. **사용자 게이트 ① (빈 슬롯 확인 + A 승인)** → Codex Phase 3-B 실서버 1회 WS 관측 실행 → Gemini가 실측 데이터를 바탕으로 `QualityPolicy`·단일 기준 점수표·손절 규칙 초안 작성.
3. **사용자 게이트 ② (비용 후보 선택·기준 정책 승인)** → `cost_schedule` 및 기준 정책 고정 → `PAPER_READY` 전환 여부 승인 후 20거래일/30회 전진 관측 진행.
