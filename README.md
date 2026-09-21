# 국내주식 투자 리서치 비서

Hermes Desktop과 Telegram이 함께 사용할 수 있는 채널 중립적 국내주식 리서치 엔진입니다. 현재 단계는 로컬 fixture 기반 MVP이며 실제 주문 기능은 없습니다.

## 현재 구현

- KOSPI·KOSDAQ 보통주와 일반 ETF 계약, 레버리지·인버스 ETF 제외
- OHLCV 검증, Wilder ATR, 추세·바닥 반등·거래량 신호
- 영업이익·OCF·FCF·회전율·희석·경영진 위험·재료 상태 필터
- 후보 5개 선정과 point-in-time event-study
- 미래에셋·토스 수동 보유정보 통합
- 매수 논리 카드와 보유·부분매도·전량매도 검토 시나리오
- 일회성 승인 토큰과 WIKI 투자일지 미리보기
- 인증된 private API와 주문 경로 강제 차단
- 역할별 API 토큰과 저장된 보고서 ID 재조회
- KRX 종목·OHLCV 백필/일일 적재와 OpenDART 기업·연간 재무 보강 명령
- OpenDART 전용 API 기반 최근 5년 유상증자·CB·BW와 희석률·자금목적·리픽싱 적재
- OpenDART 전체 재무제표의 당기·전기·전전기 표준계정으로 매출채권·재고자산 회전율 추세 계산
- OpenDART 공시목록 기반 계약·잠정실적 부분 확인과 10년 횡령·배임 공식 검토 신호
- 정규화 저장소에서 동일 기준시각으로 상위 5개 보고서를 생성하는 파이프라인
- KRX ETF NAV·순자산총액 보존과 괴리율 계산, 추적오차·총보수·집중도 결측 시 매수 보류
- 금융회사는 일반기업 OCF 규칙을 적용하지 않고 전용 건전성 계약과 전문가 검토 전까지 매수 보류
- Desktop·Telegram 공통 보고서 렌더링과 Hermes stock-research skill
- Hermes 네 개 profile용 SOUL 템플릿

## 검증

```powershell
$env:STOCK_PYTHON='C:\Users\USER\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe'
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1
```

## 로컬 확인

```powershell
$env:PYTHONPATH='src'
& $env:STOCK_PYTHON -m stock_assistant status
& $env:STOCK_PYTHON -m stock_assistant isu-case
```

실데이터 키를 파일로 준비하고 운영 변경을 승인한 뒤의 최초 적재 순서는 다음과 같습니다.

```powershell
$env:PYTHONPATH='src'
& $env:STOCK_PYTHON -m stock_assistant ingest-krx --mode backfill --calendar-days 120 --key-file '<KRX key file>'
& $env:STOCK_PYTHON -m stock_assistant enrich-dart --business-year 2025 --key-file '<OpenDART key file>'
& $env:STOCK_PYTHON -m stock_assistant enrich-dart-disclosures --key-file '<OpenDART key file>'
& $env:STOCK_PYTHON -m stock_assistant generate-candidates
```

## 아직 운영 증거가 없는 항목

- KRX·DART 실데이터 수집 명령의 실제 API 응답 검증
- 토스증권 read-only 시세·보유자산 연동
- Oracle 배포와 정기 스케줄
- 공식 Hermes Desktop 연결과 Bot Mode 토론
- Telegram 전달과 실제 WIKI 저장

이 항목들은 credential과 운영 변경 승인을 받은 후 별도로 검증합니다.

공시 제목 분류는 판결이나 계약 이행을 확정하지 않습니다. 계약·잠정실적은 `PARTIAL`, 횡령·배임 관련 제목은 `MANAGEMENT_RISK_OFFICIAL_REVIEW`로 보류 조건에만 사용합니다.
