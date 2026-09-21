# Phase 1-5 Local MVP Execution Plan

상태: Phase 0 결과 반영, 로컬 구현 승인됨

## 목표

네트워크와 운영 credential 없이 다음을 검증 가능한 코드로 만든다.

1. 국내 보통주·일반 ETF universe와 point-in-time 데이터 계약
2. 결정론적 스크리닝, 위험 필터, 후보 5개와 event-study 백테스트
3. 보유정보, 매수 논리 카드, 승인 토큰과 매도 시나리오
4. Hermes 네 개 profile 템플릿과 공통 evidence/report 계약
5. 읽기 전용 HTTP API 및 승인형 WIKI payload 생성 경계

## 파일 지도

- `src/stock_assistant/models.py`: immutable domain records and enums
- `src/stock_assistant/validation.py`: external-boundary validation
- `src/stock_assistant/indicators.py`: deterministic price and volume indicators
- `src/stock_assistant/disclosures.py`: conservative DART title-level catalyst and risk classification
- `src/stock_assistant/screening.py`: filters, scores, reason codes, top-five selection
- `src/stock_assistant/backtest.py`: point-in-time event-study replay
- `src/stock_assistant/portfolio.py`: holdings, thesis cards, scenarios
- `src/stock_assistant/approvals.py`: one-time bounded approval tokens
- `src/stock_assistant/repository.py`: SQLite schema and idempotent persistence
- `src/stock_assistant/reports.py`: shared report contract
- `src/stock_assistant/http_api.py`: localhost/private-network JSON API
- `src/stock_assistant/cli.py`: fixture and Hermes terminal adapter
- `hermes/profiles/*`: CIO, market, fundamentals, risk profile templates
- `tests/fixtures/*`: KRX, DART, news, portfolio and Isu Petasys cases

## 이유 코드

- 허용: `ELIGIBLE_COMMON`, `ELIGIBLE_ETF`
- 가격: `BOTTOM_REBOUND`, `MOMENTUM_CONTINUATION`, `VOLUME_SURGE`, `LIQUIDITY_LOW`
- 재무: `OPERATING_PROFIT_POSITIVE`, `OCF_POSITIVE`, `FCF_NEGATIVE_REVIEW`, `TURNOVER_WEAKENING`
- 자본: `DILUTION_EVENT`, `REPEATED_DILUTION`, `MANAGEMENT_RISK_CONFIRMED`
- 재료: `CATALYST_CONFIRMED`, `CATALYST_PARTIAL`, `RUMOR_ONLY`, `CATALYST_EXPIRED`
- 판단: `BUY_HOLD`, `ADDITIONAL_CHECK_REQUIRED`, `RISK_WARNING`, `DATA_UNAVAILABLE`

## 테스트 순서

1. 계약과 경계값: 정상, 빈 응답, 결측, 중복, 스키마 변경, 미래 공시
2. Wilder ATR와 이동평균/거래량 비율
3. 일반기업, 금융회사, ETF 혼용 거부
4. 필수 재무조건, 반복 희석, 경영진 위험, 재료 상태
5. 같은 입력의 동일 top-five와 이유 코드
6. 미래정보 누수 거부, 다음 거래일 체결, 거래비용 반영
7. 보유정보 통합, 추가매수 재검토, 매도 시나리오
8. 승인 만료·재사용·다른 사용자·허용 경로 이탈
9. HTTP API malformed JSON, unknown route, health, read-only enforcement
10. 이수페타시스 수동 사례가 확인된 사실과 미확인 정보를 구분하는지 확인

## 검증 명령

```powershell
$env:STOCK_PYTHON='C:\path\to\python.exe'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1
```

검증 스크립트는 `unittest`, contract fixture 검사, compile 검사, 비밀정보 패턴 검사와 `git diff --check`를 실행한다.

## 운영 연결 게이트

로컬 MVP가 통과해도 다음은 실행하지 않는다.

- Oracle 이미지 빌드·배포
- 공식 Hermes 설치·서비스 등록
- Telegram Bot token 등록·수신 경로 변경
- 실제 WIKI 작성
- KRX·DART·Toss credential 등록
- 외부 Git push

운영 연결 전 사용자는 테스트 Telegram Bot과 허용 user ID, 공식 Hermes 인증 방식, credential 등록 범위를 승인해야 한다.
