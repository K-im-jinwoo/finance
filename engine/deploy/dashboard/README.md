# Oracle 통합 읽기 전용 대시보드

별도 배포 승인 후 사용할 시스템 유닛 템플릿입니다. 검증된 커밋의 소스와 `web/dist` 빌드를 `/srv/finance-dashboard/current`에 배치하고, 설치 환경의 서비스 사용자와 Python 3.12 이상 실행 경로를 유닛에 맞춥니다. `/srv/finance-dashboard/state`를 서비스 사용자 소유로 미리 생성합니다. 조회 서비스는 자료를 읽을 수 있어야 하며, 배치는 장기 원장 디렉터리에만 쓸 수 있어야 합니다.

| 유닛 | 실행 |
| --- | --- |
| `finance-dashboard.service` | `127.0.0.1:8765`에서 상시 조회 |
| `longterm-paper.service` | 저장된 UR과 KRX 공식 일봉을 한 번 반영 |
| `longterm-paper.timer` | 평일 20:00 KST에 실행하고 누락된 예약을 재기동 후 보완 |

설치 전에 기존 서비스의 포트와 실제 KRX·UR 갱신 시간을 확인합니다. 기존 단타 조회 서비스가 8765를 사용 중이면 템플릿의 조회 포트와 SSH·프록시 대상 포트를 함께 조정합니다. 사용 중인 단타 서비스와 별개로 기동할 수 있습니다. 읽기 전용 SQLite 입력의 WAL 조회가 가능한지도 서비스 권한으로 확인합니다.

승인된 설치에서는 유닛을 시스템 유닛 디렉터리에 복사하고 `systemd-analyze verify`, `systemctl daemon-reload`, `systemctl enable --now finance-dashboard.service longterm-paper.timer` 순서로 검증·기동합니다. `journalctl`과 루프백의 `/health`, 조회 API로 동작을 확인합니다. PC 접속은 SSH 로컬 포워딩을 사용합니다. 프록시의 Host가 루프백과 다를 경우 별도로 승인한 설치에서 조회 서비스 `ExecStart`에 `--allowed-host <호스트>`를 추가합니다.

첫 배치는 생성 시점에 확보된 최신 UR 한 건부터 시작합니다. 초기 체결은 계좌 생성일과 보고서 신호 기준일이 모두 지난 거래일부터 가능합니다. 과거 체결을 확정한 뒤 늦게 확보한 일봉은 직전 확정 체결일 이전의 주문을 소급 체결하지 않습니다. 일시중지 중 신규 매수와 대기 매수 체결은 보류되며 리스크 청산과 일별 평가는 계속됩니다. 로컬이나 배치 실행 환경에서 `scripts/run_longterm_paper.py init|pause|resume|refresh`로 제어합니다.

유닛은 `STOCK_DATABASE_PATH`, `STOCK_UNIFIED_RESEARCH_DATABASE`, `DAYTRADE_STATE_ROOT`, `LONGTERM_LEDGER_PATH`로 데이터 경로를 지정합니다. 로컬 기본 경로는 저장소의 `.runtime` 아래입니다.
