# 1차 MVP 완료 조건 감사

기준일: 2026-09-21

판정 기준은 `docs/superpowers/plans/2026-09-21-hermes-stock-investment-assistant.md`의 Phase 0~6 완료 조건이다. 테스트 통과만으로 외부 연결이나 운영 상태를 완료로 간주하지 않는다.

| 단계 | 판정 | 현재 증거 | 완료에 필요한 남은 증거 |
|---|---|---|---|
| Phase 0 현황 조사 | 완료 | Oracle 자원·컨테이너·기존 custom gateway, WIKI 스키마, 공식 데이터/API 경계를 기록했고 후보 배포와 공식 Hermes 설치 직전 상태를 재확인 | 없음 |
| Phase 1 데이터 계약 | 부분 완료 | KRX·DART·뉴스 계약, fixture, 오류 분류, point-in-time 검증, KRX 종목·OHLCV·ETF NAV/순자산 적재, DART 기업·연간/중간 재무·회전율·5년 희석 이력·10년 공시목록 보강 파이프라인 | KRX·DART 키로 실제 응답 smoke, 휴장·거래정지 실데이터와 공급자 스키마 검증 |
| Phase 2 스크리닝·백테스트 | 부분 완료 | 결정론적 top 5, 미래정보 거부, 다음 거래일 체결·비용 반영, 이수페타시스 수동 fixture, 결산연도/TTM 영업이익 분리, ETF·금융회사 전용 fail-closed 계약, 희석 횟수·비율·목적·리픽싱, 계약·잠정실적 부분 확인, 횡령·배임 공식 검토와 미조회 coverage 강제 보류, 5·20·60거래일 영속 가상성과와 규칙 버전별 요약 | KRX·DART 실데이터 기간 백테스트와 충분한 표본의 성과 검증 |
| Phase 3 Hermes 투자팀 | 런타임 설치·연결 준비 | 네 SOUL, 공통 skill, 프로필별 API 정책·토큰 강제, 보고서 저장·재조회, Oracle 공식 Hermes v0.21.3 고정 설치, Desktop SSH 일회성 토큰 smoke, Windows Desktop 빌드·기동 | Desktop UI `Reachable`, 모델 로그인·일반 대화, 네 프로필 적용, Bot Mode 토론, 단일/멀티 비교 |
| Phase 4 포트폴리오·Telegram | 로컬 기능 완료 | 수동 통합 보유정보, 논리 카드·시나리오, 중복·쿨다운·전달 상태, 공통 채널 렌더링 | 별도 테스트 Bot과 허용 user ID, 실제 송수신, 재시작·실패 재전달, 세 스케줄 실행 |
| Phase 5 WIKI | 로컬 안전장치 완료 | 고정 경로, 원자적 쓰기, 내용 충돌, 일회성 승인·만료·재사용 테스트 | 임시 WIKI에서 전체 왕복 후 실제 WIKI 한 건 승인·검색·역링크 검증 |
| Phase 6 관찰 운영 | 후보 기동 | Oracle loopback 후보 API가 별도 Compose 프로젝트에서 healthy이며 권한 smoke와 기존 서비스 무변경을 확인 | 실데이터·스케줄 활성화 후 월·분기 관찰 데이터 |

## 현재 완료라고 말할 수 있는 범위

- 네트워크 없이 재현 가능한 로컬 1차 MVP와 운영 후보 구성
- 주문 기능이 없는 결정론적 분석 서비스
- 네 프로필의 독립 역할·데이터 접근 경계
- Desktop·Telegram이 공유할 수 있는 영속 report ID와 채널 렌더링
- 승인 전에는 실제 WIKI를 쓰지 않는 Writer 경계
- Oracle loopback-only 후보 API와 실제 역할별 권한 경계

## 아직 완료라고 말할 수 없는 범위

- 실제 시장 데이터로 자동 후보를 생성하는 운영 서비스
- 계약 공시의 상세 조건·현재 이행상태, DART 밖 판결·기소·제재·대주주 위험, 비표준 회전율 계정과 ETF·금융회사 전용 지표의 자동 적재
- 공식 Hermes Desktop UI의 Oracle 연결 저장·대화, Bot Mode와 Telegram의 실연결
- 수익률 개선 또는 전략 유효성
- 실제 WIKI 투자일지 왕복
- 상시 관찰 운영

## 다음 실행 게이트

운영 승인 후에도 한 번에 전환하지 않는다.

1. Desktop에 Oracle SSH 연결을 저장하고 `Test -> Reachable`과 재연결을 확인한다.
2. CIO 프로필의 모델 인증·일반 대화·stock skill을 확인한 뒤 나머지 세 프로필과 Bot Mode 토론을 검증한다.
3. 별도 Telegram Bot과 한 명의 allowlist만 연결하고 같은 report ID 왕복을 확인한다.
4. KRX·DART 실데이터 smoke 후 스케줄을 한 개씩 활성화한다.
5. 임시 WIKI에 승인형 쓰기를 검증한 뒤 실제 WIKI 한 건을 별도 승인한다.
