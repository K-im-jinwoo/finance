# Oracle 공개 시세 연결

최신 기준: **2026-09-30 16:29 KST**. 사용자 승인으로 기존 한 쌍의 ID/Secret과 공동 잠금 토큰 캐시를 수집기·공개 시세 서비스에 적용했습니다. 키 재발급·재입력 없이 공개 현재가 5종목·공식 KR 거래일 달력 조회, 수집기의 토큰 재사용이 성공했습니다. 대시보드도 직접 조회 모드로 연결됐습니다. 실행 컨테이너 두 개의 상태·재시작 0·권한 0700/0600·주문 404·Origin/외부 Host 403과 운영 DB 스키마 유지가 확인됐습니다. [적용 및 복구 기록](shared-auth-rollout.md), [배포 결과](../artifacts/oracle-shared-auth-deployment.json), [실제 조회 검증](../artifacts/oracle-shared-auth-verification.json)을 따릅니다. 실제 시가 체결·실시간 틱·다음 예약 수집 결과는 아직 검증하지 않았습니다.

아래 별도 인증 등록 안내와 미등록 상태는 이전 시점 기록입니다. 현재 기존 키를 입력 도구에 다시 넣지 않습니다.

확인 기준: 2026-09-30 15:04 KST. 사용자가 별도 인증 등록과 Oracle 연결 설정 진행을 승인했고, 이후 인증이 Oracle 환경변수에 있었던 것 같다고 알려 주어 확인했습니다.

## 확인한 인증

Oracle의 stock-assistant, Hermes 설정 및 신규 서비스 경로에서 깊이 3 이내의 환경 설정 파일 14개를 확인했습니다. `candidate.env`와 `stock-candidate-intraday-v2.env`의 `TOSS_CLIENT_ID_HOST_FILE`, `TOSS_CLIENT_SECRET_HOST_FILE`은 기존 수집기의 인증 파일을 가리킵니다. 내부 Client ID 비교 결과도 같습니다. 이 조사 범위에서는 별도 인증을 발견하지 못했습니다. 비밀값이나 비밀값 해시는 반환·보관하지 않았습니다.

토스 공식 명세는 Client ID당 유효 토큰 하나를 유지하고 재발급 시 이전 토큰을 즉시 무효화한다고 설명합니다. 따라서 기존 인증을 새 파일에 복사하거나 다른 프로세스에서 재발급하는 것으로 분리할 수 없습니다. 운영 수집기와 같은 Client ID를 쓰면 새 서비스와 등록 도구가 토큰 발급 전에 거부합니다. [토스 공식 OpenAPI 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json).

## 설치와 실제 점검

- 별도 컨테이너 `stock-dashboard-market-1`, 호스트 `127.0.0.1:9130`. 기존 검증된 이미지의 고정 digest를 사용했습니다.
- 기존 stock-assistant 컨테이너의 ID와 시작 시각이 설치 전후 같음을 확인했습니다. 운영 DB를 새 서비스에 마운트하지 않았습니다.
- 새 서비스는 소스와 인증 디렉터리를 읽기 전용으로 사용하고 주문·실계좌 경로를 구현하지 않습니다. 운영 Client ID 파일만 읽기 전용 비교에 사용하며 운영 Secret은 마운트하지 않습니다.
- 새 인증 파일 두 개는 빈 대기 파일입니다. 권한 0600, UID/GID 1001을 실제 호스트에서 확인했습니다. 현재 `/health`는 `AUTH_NOT_REGISTERED`이며, 시세 요청은 503, 주문 경로는 404, 브라우저 Origin·외부 Host는 403입니다.
- 공급자 토큰 발급·실제 인증 등록·DB 쓰기·Telegram 발송은 수행하지 않았습니다. PC 대시보드는 계속 기존 시세 저장소를 읽습니다.

증거: `artifacts/oracle-market-auth-inventory.json`, `artifacts/oracle-market-gateway-deployment.json`, `artifacts/oracle-market-gateway-verification.json`. 로컬 테스트 61개가 통과했고 기존 Hermes 연결·원문 SHA·과거 계산 실험도 재확인했습니다.

## 인증을 준비한 뒤

Windows 입력 도구 보완: PowerShell 5.1의 .NET Framework에는 `ProcessStartInfo.StandardInputEncoding`이 없어 입력 중 오류가 발생했습니다. 이 속성 대신 표준입력 파이프에 UTF-8/BOM 없는 `StreamWriter`를 직접 연결했습니다. PS1 소스는 한글 프롬프트를 PowerShell 5.1에서도 읽을 수 있도록 UTF-8 BOM으로 저장합니다. 실제 PowerShell 5.1.26100.9444에서 Client ID/Secret 기본·명시 모드, Access Token 모드, 등록 거부의 4가지 실행 경로를 로컬 Python 자식 프로세스로 검증했습니다. 더미 값의 Unicode 보존·BOM 없는 전송·민감값 없는 출력과 비공개 stderr 제외를 확인했고 SSH·공급자 호출·실제 인증 등록은 없었습니다. 이 검증은 전체 `scripts/verify.ps1`에 포함됩니다.

추가: 사용자가 인증 파일 대신 토큰을 직접 제공하겠다고 알려 주어 **이미 발급된 토스 Open API Access Token** 입력 경로도 준비했습니다. 다음 명령을 사용자가 PC에서 직접 실행하고 마스킹 입력란에 값을 붙여넣습니다. 토큰은 SSH 표준입력을 통해 Oracle의 새 서비스 인증 디렉터리에만 등록하며, PC 파일·명령행·환경변수·로그·채팅에 넣지 않습니다. 토스 웹 로그인 쿠키나 일반 앱 세션 토큰을 대신 입력하지 않습니다.

2026-09-30 15:22 KST에 Oracle의 별도 시세 서비스에 반영했습니다. 이전 별도 서비스 버전은 중지된 복구 컨테이너로 보존했고 원본 수집기는 재시작하지 않았습니다. 로컬 전체 테스트 68개·가격/재무 계산 검산·JS 문법 검사가 통과했습니다. 인증 실패·빈 토큰·헤더만 입력한 경우·잘못된 형식·자동 발급 방지·비밀값 반환 방지·파일 교체/권한 실패 정리를 검증했습니다. 실제 토큰 입력과 공급자 조회는 아직 하지 않았습니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register-market-auth.ps1 -Mode ACCESS_TOKEN
```

Access Token 경로는 Client ID/Secret 없이 공개 현재가·달력·분봉 GET 요청만 수행하며 `/oauth2/token`을 호출하지 않습니다. 만료·취소·같은 클라이언트의 다른 프로세스 재발급으로 토큰이 거부되면 오류를 표시하고 유효한 토큰 재입력이 필요합니다. 새 토큰은 사용자 입력으로만 교체합니다. 자동 발급을 제공하는 별도 Client ID/Secret 방식과 입력 토큰 방식은 서로 자동 전환하거나 덮어쓰지 않습니다. 이 단계는 단기 시세 연결용이며, 상시 인증 유지가 검증된 것으로 간주하지 않습니다. 실제 토큰 값은 아직 입력되지 않았고 공급자 연결은 미검증입니다.

자동 발급하는 방식을 선택할 경우 사용자가 토스 WTS 설정에서 **기존 운영 Client ID와 다른** Client ID/Secret을 준비해야 합니다. 발급·입력은 사용자가 직접 하며 채팅에 값을 보내지 않습니다. Oracle에 이미 별도 파일이 있다면 정확한 파일 경로만 제공하면 내부 비교 후 승인 범위 내 등록할 수 있습니다. 토스의 접근 허용 IP 설정도 Oracle의 공인 IP를 대상으로 확인해야 합니다.

사용자가 PC에서 직접 등록할 때는 프로젝트 PowerShell에서 다음 도구를 실행합니다. 두 값은 마스킹 입력이며 명령행·환경변수·PC 파일·출력에 넣지 않습니다. 기존 별도 인증과 충돌하면 덮어쓰지 않습니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register-market-auth.ps1
```

등록 뒤 전용 현재가·공식 달력·원시 분봉 응답을 실제로 검증하고, 검증이 통과한 경우에만 `scripts/serve.py --hermes-live --direct-market`으로 전환합니다. REST 30초 조회를 실시간 틱이라고 표시하지 않습니다. 별도 인증 등록 승인은 미결정 시가 관측 지연·반올림·거래 순서 정책의 확정이 아니므로, `--enable-open-fills`는 계속 비활성화합니다. 운영 수집기와 인증을 공유하도록 변경하는 대안은 원본 인증 코드와 배포를 변경해야 하므로 현재 설치에 포함하지 않았습니다.
