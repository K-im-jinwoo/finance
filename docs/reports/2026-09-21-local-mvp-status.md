# Hermes 국내주식 리서치 비서 로컬 MVP 상태

기준일: 2026-09-21

## 결론

로컬 MVP의 코드와 안전장치는 구현·검증했다. 실제 주문 기능은 없으며, Oracle 배포·공식 Hermes 설치·Desktop 연결·Telegram 알림·실제 WIKI 기록은 운영 변경 승인 전까지 실행하지 않았다.

## 확인된 사실

- 구현 브랜치: `codex/hermes-stock-mvp`
- 국내 보통주와 일반 ETF 계약, 재무·재료·기술 신호 스크리닝, 상위 5개 후보, point-in-time 이벤트 스터디, 통합 수동 포트폴리오, 매수 논리 카드, 보유/부분매도/전량매도 시나리오를 구현했다.
- HTTP API는 `/health`, 인증된 보유정보·저널 미리보기, `/v1/screen`, `/v1/candidates`, 저장된 `/v1/reports/{report_id}` 조회를 제공한다.
- CIO·시장·재무·위험·스케줄러에 서로 다른 토큰을 부여하며 전문 프로필의 보유정보·저널 접근은 서버에서 차단한다.
- Desktop과 Telegram은 저장된 같은 보고서 ID를 다시 읽고 동일한 사실·추론·가정·확인 불가 구획으로 표시할 수 있다.
- 모든 주문 경로는 `403`으로 차단하고 `orders_enabled=false`를 상태 응답에 고정했다.
- WIKI 작성기는 승인된 `wiki/20_Areas/Investments/*.md` 경로와 일회성 승인 토큰만 허용한다.
- 08:30 평일, 20:00 평일, 토요일 12:00 KST 스케줄 판정과 중복/쿨다운 알림 상태를 구현했다.
- Hermes용 `stock-cio`, `stock-market`, `stock-fundamentals`, `stock-risk` SOUL 템플릿을 만들었다.
- KRX 보통주·ETF 종목과 OHLCV의 90~366일 백필·일일 적재, OpenDART 기업코드·회사유형·연간·중간 재무 보강, 정규화 저장소 기반 상위 5개 보고서 생성을 구현했다.
- 최근 결산연도 영업이익과 중간보고서의 당기·전기동기 누적액으로 계산한 TTM 영업이익을 분리한다. 둘 중 하나가 없거나 TTM 기준일이 185일을 넘으면 후보로 승격하지 않는다.
- ETF 일별 응답의 NAV·순자산총액을 별도 point-in-time 계약으로 보존하고 괴리율을 계산한다. 추적오차·총보수·상위 구성종목 집중도가 없으면 후보로 승격하지 않으며, 레버리지·인버스 ETF는 이름 계약에서 제외 유형으로 분류한다.
- 금융회사는 일반기업의 OCF·재고·매출채권 규칙을 적용하지 않는다. 자본적정성·ROE·부실채권·연체·충당금·주주환원 전용 계약을 분리했고, 업권별 기준 검토가 끝나기 전에는 매수 보류한다.
- OpenDART 전용 주요사항 API에서 최근 5년 유상증자·CB·BW를 적재하고 희석률·자금 목적·리픽싱과 조회 coverage를 후보 판단에 연결했다. 조회하지 않은 종목은 자동으로 매수 보류한다.
- OpenDART 전체 재무제표의 당기·전기·전전기 표준계정이 존재하면 평균 매출채권·재고자산 대비 매출·매출원가로 최근 2개년 회전율 추세를 계산한다.
- DART 공시목록의 공식 접수일을 주요사항 접수번호와 대조하고, 계약·잠정실적은 부분 확인 재료로, 최근 10년 횡령·배임 제목은 유죄를 단정하지 않는 공식 검토 신호로 저장한다.
- 배포 후보는 KRX·OpenDART 키를 파일 secret으로만 마운트하고, 오전·저녁·주간 실행 스크립트는 주문 경로를 호출하지 않는다.

## 실행한 검증

- Node 테스트: 27/27 통과
- Python 테스트: 99/99 통과
- 비활성 n8n 워크플로 보안 감사: 통과
- Docker Compose 정적 구성 검증: 통과. API는 Oracle의 `127.0.0.1:9120`에만 게시하도록 제한했다.
- Python compileall, JSON fixture, 비밀정보 스캔, `git diff --check`: 통과
- 로컬 HTTP 스모크: `GET /health`가 `status=ok`, `orders_enabled=false` 반환

## 미확인 항목과 남은 위험

- KRX·OpenDART 실데이터 호출은 키가 없어 실행하지 않았다. KRX 공식 동적 명세 화면에서 주식·ETF 일별 종목코드 `ISU_CD`, ETF `NAV`·`INVSTASST_NETASST_TOTAMT` 필드를 대조했지만 실제 인증 응답 smoke는 필요하다.
- 계약의 금액·매출 대비 비중·이행/변경/해지 상세와 DART 밖 판결·기소·제재·대주주 뉴스는 아직 자동 적재하지 않는다. 비표준 재무계정 때문에 회전율을 계산할 수 없는 경우도 `확인 불가` 또는 추가확인으로 표시한다.
- ETF 추적오차·총보수·구성종목 집중도와 금융회사 전용 지표의 공식 자동 수집기는 아직 없다. 값이 없으면 `BUY_HOLD`로 실패 폐쇄한다.
- Toss Securities read-only OAuth와 실제 보유자산 동기화는 실행하지 않았다. 미래에셋은 공식 국내주식 개인용 API 경로를 확인하지 못해 MVP에서 수동 입력으로 유지한다.
- 로컬 Docker 데몬이 실행 중이 아니어서 이미지 빌드와 컨테이너 런타임 검증은 하지 못했다.
- Oracle에는 현재 공식 Hermes가 아니라 기존 커스텀 Telegram gateway가 실행 중이다. 공식 Hermes Desktop은 이 gateway에 직접 연결되지 않는다.
- Oracle 배포, 공식 Hermes 프로필 생성, ChatGPT/Codex OAuth, Desktop 원격 연결, 테스트 Telegram Bot, 실제 WIKI 기록은 실행하지 않았다.
- 공식 Hermes 문서상 OpenAI Codex는 ChatGPT OAuth를 지원하지만, 구독 등급별 사용 한도 산정 방식은 문서화되어 있지 않다. 각 프로필의 인증 상태도 독립적으로 검증해야 한다.
- 현재 성과는 구조·테스트 검증이며 실제 투자 수익률을 입증하지 않는다.

## 운영 전환 권고안

1. 기존 운영 Telegram gateway를 유지한 채 Oracle에 stock-assistant와 공식 Hermes를 별도 후보 서비스로 설치한다.
2. Desktop은 공개 포트 노출 대신 SSH 또는 Tailscale 경로를 사용한다. 최초 전환은 기존 SSH 접근을 이용하는 방식이 가장 변경 범위가 작다.
3. CIO 프로필 하나에만 별도 테스트 Telegram Bot을 연결하고, 전문 프로필은 Desktop/Bot Mode와 CIO 위임에 사용한다.
4. KRX·OpenDART 키로 실데이터 smoke를 통과한 후 08:30/20:00/토요일 12:00 스케줄을 활성화한다.
5. 동일 report ID가 Desktop과 Telegram에 표시되는지 확인한 후 실제 WIKI 쓰기를 한 건씩 승인한다.

## 다음 승인 게이트

다음 작업은 서버·외부 채널·credential·실제 WIKI를 변경하므로 별도 승인이 필요하다.

- Oracle 후보 배포와 공식 Hermes 설치
- Desktop 원격 연결 방식 확정
- 별도 테스트 Telegram Bot token과 허용 user ID 등록
- KRX·OpenDART 키 등록
- 네 개 프로필의 ChatGPT/Codex OAuth 수행
- 실제 WIKI 첫 기록 승인
