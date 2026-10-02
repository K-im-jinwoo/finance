# Git 관리 범위

2026-10-02 KST 기준으로 이 저장소는 대시보드·주식 엔진·주식 Hermes 코드, 공개·합성 검증 데이터, 인증 등록 스크립트와 비밀값을 제외한 검증 자료를 관리합니다. `.runtime`, `.work`, 인증 파일, DB, 로그, 운영 재고 조사와 화면 캡처는 로컬에 보존하고 Git에서 제외합니다. README의 운영 조사·화면 자료 링크 중 일부는 로컬에서만 사용할 수 있습니다.

`vendor/stock-assistant` 63개 파일과 `docs/reference`는 앞서 고정한 원문입니다. `.gitattributes`로 체크아웃 시 줄바꿈 변환을 차단하므로 원문 해시를 유지합니다. 최신 엔진과 뉴스 기능은 통합한 `engine/`에서 관리합니다. 원본 `wiki/.work/stock-investment-assistant` worktree는 기존 작업과 이전 근거로 보존합니다.

첫 기준 커밋은 기존 파일 말미의 빈 줄과 원문 CRLF를 보존합니다. 초기 가져오기 검사에서는 `git -c core.whitespace=-blank-at-eof,cr-at-eol diff --cached --check`를 사용하고, 이후 변경은 일반 `git diff --check`로 확인합니다.

원본 엔진의 `codex/news-discovery-cost-guard` 브랜치는 기존 HEAD에서 시작합니다. 첫 커밋은 뉴스 작업 전 이미 존재했던 코드·문서의 작업 상태를 보존하고, 후속 커밋에서 뉴스 후보 기능과 API 비용 안전장치를 구분합니다. 기존 `codex/hermes-stock-mvp` 브랜치, 작업 파일과 실제 인덱스는 유지합니다. 기준 상태 보존 커밋은 이번에 작성한 기능이라는 의미가 아닙니다.

이번 뉴스 기능은 새로운 재료를 가진 종목을 별도 검토 후보로 표시합니다. 비용 안전장치는 네이버 뉴스 호출에 일 20회·월 500회 제한, 지속 카운터, 한 시간 수집 간격, 무료 정책 확인과 불확실할 때 호출 차단을 적용합니다. 공개 정책 공지 지연까지 보장하지 않으며 다른 API·클라우드 자원의 비용은 이 제한의 대상에 포함되지 않습니다.

## GitHub 연결과 작업 기준

코드 원격은 [K-im-jinwoo/finance](https://github.com/K-im-jinwoo/finance)이며, `origin` URL은 `https://github.com/K-im-jinwoo/finance.git`입니다. 이 저장소는 공개 저장소입니다. 기준 브랜치는 `main`으로 관리하고, 기존 로컬 기준 커밋과 `codex/finance-news-safety` 브랜치를 보존합니다.

일반 변경은 최신 `main`에서 `codex/<작업명>` 브랜치를 만든 뒤 진행합니다. 작업 트리가 깨끗할 때 `git fetch origin`과 `git switch main`, `git pull --ff-only`로 기준을 갱신하고, `git switch -c codex/<작업명>`으로 작업을 시작합니다. 동시에 작업할 때는 각 브랜치의 별도 worktree를 사용하며 다른 작업의 인덱스나 변경 파일을 덮어쓰지 않습니다.

변경 후 아래 검증 명령을 실행하고 변경 파일을 명시해서 커밋합니다. 작업 브랜치를 `git push -u origin codex/<작업명>`으로 올리고, 변경과 검증 결과를 검토한 뒤 `main`에 반영합니다. GitHub 반영 완료는 로컬 커밋만으로 판단하지 않고 실제 원격 브랜치의 커밋 SHA와 로컬 SHA가 같은지 확인합니다. 강제 push와 사용자 작업의 reset/clean은 기본 절차에 포함하지 않습니다.

finance는 대시보드와 `engine/`의 주식 엔진·주식 Hermes 프로필·배포 코드까지 관리합니다. 원본 뉴스 브랜치의 33개 커밋을 squash 없이 가져왔으며, 초기 통합은 merge commit으로 반영합니다. 가져온 원본 커밋 ID와 통합·검증 절차는 [주식 엔진 통합](stock-engine-integration.md)에 기록합니다. `vendor/stock-assistant`는 검증용 고정 스냅샷으로 보존합니다.

인증 키, 토큰, 환경 파일, DB와 운영 로그는 계속 Git에서 제외합니다. 공개 push 전에 비밀값을 점검하고, 실제 비밀값이 커밋되면 파일 삭제만으로 해결하지 않고 키 폐기·재발급과 이력 정리를 별도로 처리합니다.

GitHub 연결 및 push는 운영 배포와 별개의 작업입니다. 이 저장소의 자동 배포는 구성하지 않았으며 서버에 변경을 적용할 때는 승인된 대상과 커밋을 따로 확인합니다.

테스트는 Git에 포함된 파일과 합성 운영 조사 메타데이터만으로 실행할 수 있습니다. 실제 운영 조사 파일을 Git에 올리거나 서버를 조회하지 않고 상태 API의 검증 범위 표시를 확인합니다.

검증 명령:

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -Python 'Python 3.12 이상 실행 파일의 절대경로'
git status --short
git log --oneline -5
```
