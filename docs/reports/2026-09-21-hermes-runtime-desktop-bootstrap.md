# Official Hermes Runtime and Desktop Bootstrap

기준일: 2026-09-21 KST

## 결론

Oracle과 Windows에 공식 Hermes Agent `v0.21.3 (2026.9.14)`을 준비했다. Oracle은 태그 커밋 `345cd2b057a452236de401d3534b8502a7465e8d`에 고정했고, Windows Desktop은 로컬 설치 소스에서 패키징하여 실행했다. Desktop SSH 연결과 동일한 일회성 토큰 방식의 백엔드 smoke는 통과했지만, Desktop UI에서 연결을 저장하고 `Test` 버튼으로 `Reachable`을 확인하는 단계는 아직 수행하지 못했다.

## Oracle에서 확인한 사실

- 설치 경로: `/home/ubuntu/.hermes/hermes-agent`
- 실행 경로: `/home/ubuntu/.local/bin/hermes`
- 버전: `Hermes Agent v0.21.3 (2026.9.14)`
- 설치 커밋과 태그: `345cd2b057a452236de401d3534b8502a7465e8d`, `v2026.9.14`
- `.env`와 `config.yaml` 권한: `600`
- 브라우저 자동화, Computer Use, setup wizard는 설치에서 제외했다.
- 상시 9119 리스너를 만들지 않았다. Desktop SSH가 필요할 때 loopback 백엔드를 시작하는 방식을 사용한다.
- 기존 `deploy-hermes-gateway-1`, `deploy-wiki-agent-1`, `stock-assistant-stock-assistant-1`은 모두 `healthy`, 재시작 횟수 0을 유지했다.

## Desktop SSH 호환성 smoke

공식 Desktop이 사용하는 형식으로 64자리 일회성 토큰 파일과 16자리 owner nonce를 만들고 `hermes serve --isolated --host 127.0.0.1`을 임시 실행했다.

- `/api/health`: `ok=true`, `version=0.21.3`, `auth_required=false`
- 토큰 없는 `/api/ssh/ownership`: HTTP 401
- 올바른 `X-Hermes-Session-Token`: `ok=true`, owner nonce 일치, `protocolVersion=1`, `runtimeIntact=true`
- 토큰 파일이 기동 시 소비되어 제거되는 것을 확인했다.
- 테스트 프로세스 종료 후 9119 리스너가 사라진 것을 재확인했다.

## Windows Desktop

- 로컬 Hermes CLI 버전: `v0.21.3 (2026.9.14)`
- 생성된 실행물: `C:\Users\USER\AppData\Local\hermes\hermes-agent\apps\desktop\release\win-unpacked\Hermes.exe`
- Desktop 프로세스와 로컬 Hermes 백엔드 WebSocket 연결을 확인했다.
- 빌드 중 npm은 12개 취약점(낮음 1, 보통 5, 높음 6)을 보고했다. 자동 `npm audit fix`는 실행하지 않았다.
- 로컬 Hermes 소스는 패키징 전에 이미 dirty 상태로 표시됐다. 사용자 변경 여부를 확인하지 않았으므로 되돌리지 않았다.

## Desktop에 입력할 값

`Settings -> Gateways -> Add connection -> SSH`에서 다음 값을 사용한다.

- Name: `Oracle Stock Hermes`
- SSH host: `ubuntu@144.24.92.159:22`
- Hermes path: `/home/ubuntu/.local/bin/hermes`
- SSH key: `C:\Users\USER\ssh-key-2026-07-08.key`

저장한 뒤 `Test`가 `Reachable`을 반환해야 Gate 3의 Desktop 실연결이 완료된다. 모델 인증이 없으므로 연결 성공만으로 실제 대화 성공을 의미하지 않는다.

## 아직 하지 않은 작업

- Desktop UI의 SSH 연결 저장과 `Reachable` 확인
- 모델 로그인과 일반 대화 smoke
- 네 투자 프로필 생성 및 Bot Mode 토론
- Telegram, KRX, OpenDART, WIKI 연결
