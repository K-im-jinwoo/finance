# 주식 엔진 통합

2026-10-02 KST 기준으로 finance가 주식 엔진·주식 Hermes 프로필·대시보드·모의계좌의 소스 관리 기준입니다. WIKI 비서와 공통 Hermes 운영 코드는 `hermes-ops`에서 관리합니다.

## 구조와 실행

기존 대시보드의 `src/research_dashboard`, `web`, `scripts`, `tests` 구조를 유지합니다. 원본 엔진의 소스·테스트·Hermes 프로필·배포 파일은 `engine/` 안에 있습니다. 대시보드 스크립트와 테스트는 `engine/src`를 참조하고, 루트 Python 패키지도 해당 소스를 포함합니다. 엔진 단독 패키지와 `python -m stock_assistant` 진입점도 유지합니다.

`vendor/stock-assistant` 63개 파일과 `docs/reference`는 기존 검증의 원문입니다. 이후 개발 코드는 `engine/`에서 수정하고 이 스냅샷은 변경하지 않습니다. `datasets/validation.json` 등 기존 입력과 검증 자료의 원본도 유지합니다. 새로 생성하는 합성 자료는 새 엔진 이력을 기록합니다.

## 가져온 이력과 원본 보존

`codex/news-discovery-cost-guard`의 `e3be6821d2c60e1a8a6b1a98d3a430dc247ed3f6`까지 33개 커밋과 130개 파일을 `git subtree add --prefix=engine`으로 가져왔습니다. squash를 사용하지 않아 원본 커밋 ID가 통합 저장소의 조상 이력에 포함됩니다. 가져온 커밋과 tree는 [manifest](stock-engine-import.json)에 기록합니다.

기존 원본 Git 저장소의 모든 refs는 로컬 bundle로 보관하고 활성 worktree의 추적·미추적 작업 파일 및 인덱스는 별도 압축본과 SHA-256 manifest로 보존합니다. 위치는 Git에서 제외된 `.work/integration-backups/`이며 GitHub에는 업로드하지 않습니다. 원본 worktree·브랜치·인덱스·파일은 변경하지 않습니다. 기존 workspace의 생성된 egg-info와 `uv.lock`도 백업하되 통합 소스로 가져오지 않습니다.

이후 작업은 finance의 `engine/`에서 시작합니다. 기존 원본에서 진행 중인 별도 작업이 있으면 변경을 비교해 명시적으로 가져옵니다. 두 위치를 자동 동기화하거나 양쪽을 계속 수정하는 방식은 사용하지 않습니다.

## 검증

`verify_engine_integration.py`는 별도 Python 프로세스에서 기존 스냅샷과 새 엔진을 각각 불러옵니다. 같은 입력의 합성 선택·사건, 원래 저장 입력 및 새 생성 입력의 모의계좌 재생, 공개 가격·재무 계산을 비교합니다. 외부 API를 호출하지 않고 기존 검증 자료를 덮어쓰지 않습니다.

루트 `scripts/verify.ps1`은 이 비교, 대시보드 테스트, PowerShell 인증 입력 시험, JavaScript 문법, 엔진의 Node/Python 전체 검증 및 워크플로 감사, 설치된 Docker CLI의 Compose 설정 검사와 Git 공백 검사를 실행합니다. Linux 권한 검증은 Windows에서 건너뛰며 운영 배포나 실제 시장 동작을 증명하지 않습니다.

## 확인한 결과

새 checkout에서 대시보드 89개 중 87개 통과·Linux 전용 2개 건너뜀, 엔진 Python 181개 통과, Node 27개 통과, 합성 PowerShell 인증 입력 4건 통과를 확인했습니다. 워크플로 감사·JavaScript 문법·Compose 설정·공백 검사도 통과했습니다. 통합 wheel은 외부 패키지 설치 없이 만들었으며 대시보드와 최신 뉴스 엔진을 포함한 43개 Python 모듈의 포함 여부와 wheel에서의 import를 확인했습니다.

기존 스냅샷과 새 엔진의 합성 사건 29개, 모의계좌 재생, 가격 계산 날짜 125개와 재무 검산 11건이 같습니다. 원본 135개 작업 파일과 HEAD·브랜치·인덱스·refs·상태, 고정 vendor·원문 문서·기존 입력·검증 자료의 보존을 대조했습니다. [검증 기록](../artifacts/stock-engine-integration-verification.json)에 검증한 코드 커밋과 범위를 기록합니다.

전체 검증 중 기존 공개 시세 서비스의 Windows 거절 응답 연결 오류가 재현돼, 거절한 POST 본문을 한도 내에서 소비한 후 HTTP 403/404를 전달하도록 보완했습니다. 허용 요청의 2048바이트 제한과 인증·호스트·주문 차단 정책은 유지합니다. 회귀 검증은 작은 본문과 4096바이트 문자열 본문을 포함합니다. 운영 서버에는 적용하지 않았습니다.

Docker 이미지 빌드·운영 배포·실제 시장 동작은 이번 검증의 대상에 포함되지 않습니다.

## 배포 경계

엔진의 Docker 빌드 context는 `engine/`이고 Dockerfile은 `engine/deploy/Dockerfile`입니다. `engine/deploy/compose.yaml`의 기존 상대 경로와 실행 모듈을 유지합니다. 서버 DB·자격증명·프로필 상태 경로는 이 통합으로 이동하지 않습니다.

GitHub 통합은 서버 적용과 별개입니다. 자동 배포, 운영 재시작, DB 변경, 실제 주문 및 Telegram 발송을 구성하거나 수행하지 않습니다. 배포 시에는 검증한 finance 커밋과 기존 롤백 대상을 따로 확인합니다.
