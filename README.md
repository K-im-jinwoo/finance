# 주식 리서치 대시보드

Hermes 투자총괄의 **실제 최종 추천과 원문 보고서**를 읽는 로컬 모의계좌와, 앞서 만든 합성 계산 검증 실험을 제공합니다. 최신 Hermes 최종판정의 명시적 1순위만 신규 편입 신호로 사용합니다. 2026-09-30 16:25 KST, 사용자 승인에 따라 기존 토스 ID/Secret과 공유 토큰 캐시를 운영 수집기·공개 시세 서비스에 적용했습니다. 키 재발급·재입력 없이 실제 5종목 현재가와 공식 거래일 달력 조회, 수집기의 동일 토큰 재사용을 확인했습니다. 대시보드는 **공개 REST 30초 조회** 모드입니다. 실제 시가 관측·가상 체결은 정책 확인과 장 시작 검증 전 보류하며, 실시간 틱·실제 3년 백테스트·실주문·Telegram 발송은 검증하지 않았습니다. 운영 DB 스키마는 적용 전후 같고, 수집 CLI를 시험 실행하지 않았습니다.

기준: 2026-09-30 KST. 작업 위치는 사용자가 선택한 `finance` 프로젝트입니다.

코드 저장소: [K-im-jinwoo/finance](https://github.com/K-im-jinwoo/finance). 기준 브랜치는 `main`이며, GitHub 연결과 작업 절차는 [Git 관리 범위](docs/git-management.md)를 참고합니다.

2026-10-02 추가 작업: 뉴스의 새로운 재료로 별도 검토 후보를 만들고 네이버 뉴스 API에 일 20회·월 500회 비용 안전장치를 적용했습니다. [Git 관리 범위와 원본 엔진 이력](docs/git-management.md), [비밀값을 제외한 운영 검증 결과](artifacts/news-api-cost-guard-verification.json)를 참고합니다. 아래 대시보드 설명과 검증 범위는 2026-09-30 당시 기준입니다.

- [데이터 확보 조사](docs/data-coverage.md): 실제 확보/공식 제공 가능/미확인을 구분합니다.
- [규칙 명세와 검증 시나리오](docs/rules-and-validation.md): 사용자 확정과 구현 제안을 구분합니다.
- [진행 상태와 후속 통과 조건](docs/progress.md)
- [Hermes 추천 모의계좌 연결과 검증](docs/hermes-paper-account.md)
- [Oracle 별도 시세 서비스와 인증 확인](docs/oracle-market-connection.md)
- [기존 키 공유 인증 변경안과 적용 대상](docs/shared-auth-rollout.md)
- [원본 현재 작업 트리·해시](artifacts/source-audit.json)
- [Oracle 실제 저장 범위 집계](artifacts/oracle-data-coverage.json), [실제 공급자 표본](datasets/observed-provider-sample.json)
- [합성 계산 검증 결과](artifacts/validation-results.json)
- [실제 5종목×85거래일 자료, 25개 계산 날짜×9개 지표 검산](artifacts/observed-price-calculation.json)
- [실제 1종목·3기간 재무 및 저장 스냅샷 11개 대조](artifacts/observed-financial-calculation.json)

Python 3.12+, Node(문법 검사용)가 필요합니다. Python 라이브러리 의존성은 없습니다.

```powershell
$taskPython = 'C:\Users\USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -Python $taskPython
& $taskPython scripts\run_experiment.py --compare
& $taskPython scripts\serve.py
```

승인해 적용한 공유 인증의 실제 Hermes 보고서·공개 시세 연결은 `& $taskPython scripts\serve.py --hermes-live --direct-market`로 실행하며, `http://127.0.0.1:8765/hermes`에서 확인합니다. 기존 SSH 설정을 사용하고 PC에는 키·토큰을 반환하지 않습니다. 원격 서비스의 전용 잠금 캐시가 필요한 토큰 발급·갱신을 공동 처리합니다. 최초 연결 시 별도 로컬 계좌를 만들며 기존 계좌가 있으면 이어서 조회합니다. 현재가가 5분보다 오래되면 현재 손익·수익률을 확인 불가로 표시합니다. `--enable-open-fills`는 미확정 체결 정책을 별도로 확인하고 실제 장 시작 자료를 검증한 뒤 사용합니다. 기존 저장소 조회 모드는 `--direct-market`을 생략해 선택합니다.

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 생성한 검증 실험은 시작 후 합성 이벤트를 시간순으로 자동 계산합니다. 일시중지 중에도 입력과 평가가 진행되며 거래 대기는 취소합니다. 실행 중 서버를 재시작하면 저장된 커서에서 이어집니다. 완료·실패한 실험은 재시작하지 않습니다. 설정 변경은 새 실험입니다.

휴대폰 폭 화면은 반응형으로 구현했지만 서버는 loopback에만 열려 있습니다. 실제 휴대폰 외부 접속, 공개 로그인, 서비스 배포는 미검증·별도 승인 범위입니다. 현재 세션은 서버 메모리의 임시 HttpOnly 쿠키이고, 비밀키를 브라우저에 넣지 않습니다.

실제 원본 DB가 로컬 파일로 제공되면 아래 명령은 가격·재무·공시 테이블의 **집계만 읽습니다**. 운영 DB를 복사하거나 초기화하지 않습니다. 자격증명이나 실제 보유 상세는 조회하지 않습니다.

```powershell
& $taskPython scripts\audit_market_db.py '사용자가 제공한 DB 절대경로'
```

정규화 이벤트 입력 계약은 `datasets/validation.json`과 `src/research_dashboard/contracts.py`를 참고합니다. 실제 데이터 검증 입력은 `mode=OBSERVED`로 분리하며 공개·관측·가용시각을 모두 보존해야 합니다. 임의로 관측시각을 과거로 바꾸면 안 됩니다. `run_experiment.py --dataset 경로`는 제안 규칙에 대한 로컬 계산이며, 이를 승인된 전략의 3년 실적이라고 표시하지 않습니다. 관측 데이터의 과거 실험 자동 재생은 차단돼 있습니다. Hermes 계좌는 별도의 앞으로 발생하는 사건 원장입니다.

원본 재사용은 `vendor/stock-assistant`의 현재 파일 스냅샷으로 고정했습니다. 원본의 변경 파일·미추적 모듈을 포함한 소스와 Python 테스트/fixture 63개를 SHA-256으로 대조했습니다. DB·환경 파일·키·보유정보·로그·가상환경은 복사하지 않았습니다. 계산에서 실제 재사용한 함수는 `screen_security`, `select_top_candidates`와 모델 계약입니다. `repository`, HTTP API, 공급자 모듈은 계약 참고용이며 화면은 원본 API를 호출하거나 원본 저장소를 생성하지 않습니다. 기존 `event_study`의 다음 날짜 진입과 새 시각 기준 체결은 별개로 유지합니다.

`docs/reference`의 인수인계 두 문서는 원문 보관본입니다. 상대 링크는 원본 프로젝트 기준이며, 새 조사·결정은 다른 파일에 기록합니다. 원본 보고서·완료 성과는 수정하지 않았습니다.

