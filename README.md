# 국내주식 투자 리서치 비서

Hermes Desktop과 Telegram이 함께 사용할 수 있는 채널 중립적 국내주식 리서치 엔진입니다. 현재 단계는 로컬 fixture 기반 MVP이며 실제 주문 기능은 없습니다.

## 현재 구현

- KOSPI·KOSDAQ 보통주와 일반 ETF 계약
- OHLCV 검증, Wilder ATR, 추세·바닥 반등·거래량 신호
- 영업이익·OCF·FCF·회전율·희석·경영진 위험·재료 상태 필터
- 후보 5개 선정과 point-in-time event-study
- 미래에셋·토스 수동 보유정보 통합
- 매수 논리 카드와 보유·부분매도·전량매도 검토 시나리오
- 일회성 승인 토큰과 WIKI 투자일지 미리보기
- 인증된 private API와 주문 경로 강제 차단
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

## 아직 운영 증거가 없는 항목

- KRX·DART 실데이터 수집
- 토스증권 read-only 시세·보유자산 연동
- Oracle 배포와 정기 스케줄
- 공식 Hermes Desktop 연결과 Bot Mode 토론
- Telegram 전달과 실제 WIKI 저장

이 항목들은 credential과 운영 변경 승인을 받은 후 별도로 검증합니다.

