# 기존 토스 인증을 공유하는 변경안

## 승인 후 적용 및 실제 검증

**2026-09-30 16:25 KST 적용, 16:29 KST 실제 조회 확인.** 사용자의 `진행` 승인으로 두 서비스를 공유 캐시 모드로 전환했습니다. 기존 ID/Secret 파일을 그대로 읽고 첫 Access Token만 공동 발급했으며, 뒤이은 수집기/Compose 실행 경로/시세 서비스 요청은 같은 토큰을 재사용해 캐시 파일이 바뀌지 않았습니다. 이는 이번 확인 구간의 재사용 증거이며 실제 만료 시각의 갱신은 아직 관측하지 않았습니다. 동시 갱신·만료·401 처리는 합성 테스트와 실제 Linux 잠금 테스트로 검증했습니다.

- 기존 v10 이미지 digest·DB/인증 마운트·명령·기존 환경·로그·리소스·네트워크·헬스체크를 Docker Engine 설정 복제로 보존했습니다. 인증 환경변수 하나와 코드/캐시 마운트만 추가한 새 수집 컨테이너를 시작했고 기존 실행 파일의 단일 명령을 위의 공유 진입점으로 바꿨습니다. 기존 Compose `exec`가 새 수집기를 선택하고 공개 조회를 실행하는 것도 확인했습니다.
- `stock-dashboard-market-1`은 localhost 9130, 기존 인증 두 파일 읽기 전용, 코드 읽기 전용, 전용 인증 캐시만 쓰기 가능입니다. 운영 DB/실계좌/주문 엔드포인트는 연결하지 않습니다. 이미지에 남아 있던 Compose 수집 서비스 라벨은 새 서비스에서 다른 project/service로 덮어 기존 수집 경로가 새 서비스를 선택하지 않게 했습니다.
- 5종목 공개 가격과 공식 정규장 달력, 양쪽 동일 토큰 재사용, 캐시 0700/파일 0600 및 UID/GID 1001, 기존 API/새 서비스 건강 상태와 재시작 0을 확인했습니다. 주문 404·브라우저 Origin/외부 Host 403, 외부 Hermes 알림 래퍼 해시 유지, 운영 DB 스키마 유지가 통과했습니다. 수집 CLI·실제 메시지·실주문은 시험하지 않았습니다. 다음 정규 예약 수집의 결과는 별도 실관측이 필요합니다.
- 적용 결과는 `artifacts/oracle-shared-auth-deployment.json`, 실제 조회는 `artifacts/oracle-shared-auth-verification.json`입니다. PC 서버도 기존 계좌를 유지해 DIRECT로 전환했고 `--enable-open-fills`는 사용하지 않았습니다.

최종 로컬 검증은 79개 실행·오류/실패 0이며 POSIX 권한 1개는 Windows에서 제외했습니다. 해당 테스트를 포함한 9개 공유 인증 테스트는 적용된 Linux 서비스의 임시 디렉터리에서 전부 통과했습니다. 실제 PowerShell 5.1 입력 전송 4개 경로, 실제 가격 125개 계산 날짜×9개 지표·재무 11개 대조, JS 문법·원본 보존 검증도 통과했습니다. 첫 로컬 전체 실행에서 Windows의 HTTP 연결 중단 오류 한 건이 발생했으나 해당 모듈 독립 실행과 두 차례 전체 재실행에서는 재현되지 않았습니다. 공급자 실제 장 시작·만료 갱신·다음 예약 수집·복구 실행 시험은 남아 있습니다.

복구본:

```text
수집 컨테이너: stock-assistant-stock-assistant-1-backup-20260930T072500
시세 컨테이너: stock-dashboard-market-1-backup-20260930T072500
기존 수집 래퍼: /srv/stock-dashboard/backups/20260930T072500/run_intraday_watch.sh
신규 릴리스: /srv/stock-dashboard/releases/shared-4005ef7d88135f0a6c7e
기존 시세 릴리스: /srv/stock-dashboard/releases/0db275a6cbb88b17351b
```

복구 컨테이너는 정지·restart=no 상태로 보관해 중복 실행을 막습니다. 추후 수집 컨테이너를 Compose로 다시 만들 때는 기존 compose/env에 신규 릴리스의 `collector.override.yaml`을 함께 지정해야 합니다. 현재 원본 compose 파일 자체는 바꾸지 않았으므로 base만 사용해 재생성하면 새 마운트가 빠집니다. 복구는 두 새 서비스를 정지한 뒤 원본 이름을 복구하고 기존 restart 정책과 래퍼·시세 current 포인터를 되돌리는 방식이며, 실제 복구 실행 시험은 하지 않았습니다. 승인 없이 복구·다른 운영 변경을 자동 수행하지 않습니다.

아래는 승인 전 준비·조사 기록입니다. 현재 적용 상태는 위 기록을 따릅니다.

2026-09-30 KST. 사용자가 Client ID/Secret은 한 쌍만 발급 가능하고 여러 곳에서 쓰려는 것이라고 확인했습니다. 별도 Client ID 발급·키 재발급·사용자 재입력 없이 기존 Oracle 파일을 그대로 사용하는 방식으로 수정합니다. 이전 문서의 '별도 Client ID 필수'는 독립적으로 토큰을 발급하던 초기 구현의 제한이며 공급자가 정한 여러 서비스 사용 금지가 아닙니다.

## 확인한 차이

WTS에서 ID/Secret을 재발급하는 것과, 동일한 ID/Secret으로 Access Token을 발급하는 것은 다릅니다. 전자는 인증 키 교체이며 이번 변경에는 필요하지 않습니다. 공식 명세는 Access Token 발급 시 같은 클라이언트의 이전 토큰이 무효화된다고 명시합니다. 여러 서비스가 유효한 같은 토큰을 사용하는 것은 가능하지만 독립 발급을 반복하면 충돌합니다. [토스 공식 OpenAPI 명세](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json).

원본 `refresh-intraday`는 실행마다 `TossMarketDataClient`를 새로 만들며 캐시는 해당 프로세스 메모리에만 있습니다. 실제 운영 릴리스의 `deploy/jobs/run_intraday_watch.sh`에도 기존 실행 명령이 정확히 한 번 있습니다. 원본 소스·Git 상태는 변경하지 않았습니다. 호스트 조사 결과는 `artifacts/oracle-shared-auth-hook-inventory.json`입니다.

## 준비한 코드와 적용 대상

| 준비한 파일 | 동작과 적용 위치 |
| --- | --- |
| `scripts/shared_toss_auth.py` | 같은 키의 토큰을 전용 캐시에 저장합니다. 프로세스 간 파일 잠금·만료 전 갱신·오래된 요청의 401이 새 토큰을 폐기하지 않도록 비교 후 무효화합니다. 키는 기존 파일에서 읽고 복사·변경하지 않습니다. |
| `scripts/shared_collector_entry.py` | 기존 `refresh-intraday` CLI에 공유 인증 공급자만 연결합니다. 원본 계산·DB 경로·기존 알림 판정과 매개변수는 재사용합니다. 다른 CLI 명령은 거부합니다. |
| `scripts/shared_gateway_entry.py` | 기존 ID/Secret을 읽기 전용으로 읽고 동일한 토큰 캐시로 공개 시세·공식 달력·원시 분봉만 제공합니다. 토큰/키를 PC에 반환하지 않습니다. |
| `deploy/shared-auth/collector.override.yaml` | 기존 운영 컨테이너에 새 코드 읽기 전용 마운트와 전용 인증 캐시 마운트·환경변수 하나를 추가하는 준비용 override입니다. 현재 적용하지 않았습니다. |

수집기의 단일 명령 변경:

```text
현재: python -m stock_assistant refresh-intraday
변경: python /opt/stock-shared-auth/shared_collector_entry.py refresh-intraday
```

현재 확인한 운영 파일:

```text
/srv/stock-assistant/releases/f3af37a-wip-20260928-intraday-dedup-v10/deploy/jobs/run_intraday_watch.sh
SHA256: 25ca73279a0a61b3ac17654655f928396ef80341b5ea7802991bf9c549b0cb1e
```

컨테이너는 기존 v10 이미지와 기존 인증·DB 마운트를 유지하고, 마운트 추가 때문에 한 번 재생성해야 합니다. 원본 이미지·소스·인증 파일을 교체하지 않습니다. 새 시세 서비스도 기존 키의 읽기 전용 마운트 및 같은 캐시 마운트가 필요합니다. 인증 캐시 디렉터리는 운영 DB와 분리하며 UID 1001, 권한 0700, 토큰/잠금 파일 0600입니다. 캐시에는 토큰이 들어 있으므로 백업 로그·Git·PC 출력에 포함하지 않습니다.

## 검증과 적용 순서

로컬 테스트는 다른 클라이언트 객체·동시 스레드·동시 프로세스가 같은 토큰을 사용해 발급이 한 번만 발생하는지 확인합니다. 만료 갱신·401 처리·클라이언트 불일치·캐시 손상·잘못된 공급자 응답은 별도 검증합니다. 실제 Linux의 신규 시세 컨테이너 임시 파일시스템에서 동일한 9개 테스트를 실행해 전부 통과했고, POSIX 디렉터리·파일 권한도 확인했습니다. 실제 토큰 발급·운영 DB 쓰기·Telegram 발송은 테스트하지 않았습니다. 결과는 `artifacts/shared-auth-linux-verification.json`입니다.

운영 적용은 기존 수집기 재생성과 실행 경로 변경을 포함하므로, 이전의 **신규 별도 서비스 설치 승인**과 구분해 대상 확인 후 진행합니다. 적용 시에는 기존 파일/설정·컨테이너 식별을 보존하고 해당 수집 경로를 잠시 중지한 뒤, 수집기와 새 시세 서비스 양쪽을 공유 캐시 모드로 전환합니다. 이전 모드 수집 프로세스가 남아 독립적으로 토큰을 발급하지 않도록 확인해야 합니다. 고의 알림 발생이나 실제 메시지 발송 시험은 하지 않습니다. 공개 현재가 조회와 정상 수집 결과·오류 여부를 확인한 뒤 PC 대시보드를 직접 조회 모드로 전환합니다.

승인 전에는 공유 키 프로필을 활성화하거나 기존 키로 토큰을 발급하지 않습니다. 현재 등록 도구로 기존 ID/Secret을 다시 입력할 필요가 없습니다. 실제 시가 관측 정책과 미결정 운용 규칙도 이 인증 변경으로 자동 확정하지 않습니다.
