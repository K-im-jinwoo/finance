# Oracle Stock Assistant Candidate Deployment

기준일: 2026-09-21 KST

## 배포 결과

- 배포 커밋: `08c10af`
- Oracle 호스트: `jw1`, Ubuntu 24.04 arm64
- 릴리스 경로: `/srv/stock-assistant/releases/08c10af`
- Compose 프로젝트: `stock-assistant`
- 컨테이너: `stock-assistant-stock-assistant-1`
- 게시 주소: `127.0.0.1:9120`만 사용
- 컨테이너 상태: `healthy`, 재시작 0회, read-only root filesystem
- 영속 상태: `/srv/stock-assistant/state/stock-assistant.sqlite3`

역할 토큰 다섯 개는 Oracle 내부에서 생성했고 값은 출력하거나 로컬로 복사하지 않았다. KRX와 OpenDART 키 파일은 길이 0의 placeholder이며 실데이터 호출은 실행하지 않았다.

## 실제 smoke 결과

- 공개 `/health`: 200, `orders_enabled=false`
- 미인증 `/v1/holdings`: 401
- CIO 인증 주문 경로: 403
- 시장 전문가 인증 보유정보: 403
- CIO fixture 후보 보고서 생성: 성공
- 시장 전문가가 동일 report ID 조회: 성공
- smoke report ID: `R-20260920T1200Z-2518FC67`

기존 `deploy-hermes-gateway-1`과 `deploy-wiki-agent-1`은 별도 `deploy` 프로젝트에서 계속 `healthy`였고 재시작 횟수는 모두 0이었다. Telegram, WIKI, n8n과 증권 주문 상태는 변경하지 않았다.

## 아직 활성화하지 않은 항목

- KRX·OpenDART 실데이터 수집
- 후보 생성 스케줄
- 공식 Hermes runtime과 네 프로필
- Desktop SSH 세션
- 테스트 Telegram Bot
- WIKI 실제 쓰기

## 롤백

릴리스와 상태 DB는 보존하고 후보 컨테이너만 중지한다.

```bash
cd /srv/stock-assistant/releases/08c10af
docker compose --env-file deploy/candidate.env -f deploy/compose.yaml --profile candidate stop stock-assistant
```
