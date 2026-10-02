# Git 관리 범위

2026-10-02 KST 기준으로 이 저장소는 대시보드 코드, 공개·합성 검증 데이터, 인증 등록 스크립트와 비밀값을 제외한 검증 자료를 관리합니다. `.runtime`, `.work`, 인증 파일, DB, 로그, 운영 재고 조사와 화면 캡처는 로컬에 보존하고 Git에서 제외합니다. README의 운영 조사·화면 자료 링크 중 일부는 로컬에서만 사용할 수 있습니다.

`vendor/stock-assistant` 63개 파일과 `docs/reference`는 앞서 고정한 원문입니다. `.gitattributes`로 체크아웃 시 줄바꿈 변환을 차단하므로 원문 해시를 유지합니다. 현재 뉴스 기능의 소스는 이 스냅샷과 별도로 원본 `wiki/.work/stock-investment-assistant` 저장소에서 관리합니다.

첫 기준 커밋은 기존 파일 말미의 빈 줄과 원문 CRLF를 보존합니다. 초기 가져오기 검사에서는 `git -c core.whitespace=-blank-at-eof,cr-at-eol diff --cached --check`를 사용하고, 이후 변경은 일반 `git diff --check`로 확인합니다.

원본 엔진의 `codex/news-discovery-cost-guard` 브랜치는 기존 HEAD에서 시작합니다. 첫 커밋은 뉴스 작업 전 이미 존재했던 코드·문서의 작업 상태를 보존하고, 후속 커밋에서 뉴스 후보 기능과 API 비용 안전장치를 구분합니다. 기존 `codex/hermes-stock-mvp` 브랜치, 작업 파일과 실제 인덱스는 유지합니다. 기준 상태 보존 커밋은 이번에 작성한 기능이라는 의미가 아닙니다.

이번 뉴스 기능은 새로운 재료를 가진 종목을 별도 검토 후보로 표시합니다. 비용 안전장치는 네이버 뉴스 호출에 일 20회·월 500회 제한, 지속 카운터, 한 시간 수집 간격, 무료 정책 확인과 불확실할 때 호출 차단을 적용합니다. 공개 정책 공지 지연까지 보장하지 않으며 다른 API·클라우드 자원의 비용은 이 제한의 대상에 포함되지 않습니다.

원격 저장소는 아직 등록하지 않았습니다. 이 작업은 로컬 커밋이며 외부 push와 운영 재배포를 수행하지 않습니다.

검증 명령:

```powershell
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1 -Python 'Python 3.12 이상 실행 파일의 절대경로'
git status --short
git log --oneline -5
```
