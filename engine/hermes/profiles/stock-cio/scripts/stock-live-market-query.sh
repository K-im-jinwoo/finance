#!/bin/sh
set -eu

symbols="${1:-}"
if [ -z "$symbols" ]; then
  echo "one to five comma-separated six-character symbols are required" >&2
  exit 2
fi

case "$symbols" in
  *[!0-9A-Z,]*|,*|*,|*,,*)
    echo "symbols must be comma-separated six-character uppercase alphanumeric codes" >&2
    exit 2
    ;;
esac

old_ifs=$IFS
IFS=,
set -- $symbols
IFS=$old_ifs
if [ "$#" -lt 1 ] || [ "$#" -gt 5 ]; then
  echo "one to five symbols are required" >&2
  exit 2
fi
for symbol do
  if [ "${#symbol}" -ne 6 ]; then
    echo "each symbol must contain exactly six characters" >&2
    exit 2
  fi
done

prompt="CIO가 요청한 국내주식 종목코드 $symbols 의 현재가와 장중 거래량을 확인하라. 지정된 읽기 전용 스크립트를 전체 종목 묶음에 정확히 한 번만 실행하고, 다른 도구·에이전트·웹·저장 종가를 사용하지 마라. 판정하지 말고 종목명, 종목코드, 현재가, KST 관측시각, 신선도, 최근 완성 1분봉 거래량 배율, 경고, 출처만 한 번의 최종 메모로 반환하라."

stderr_file=$(mktemp)
trap 'rm -f "$stderr_file"' EXIT HUP INT TERM

set +e
result=$(timeout 65s /home/ubuntu/.local/bin/hermes -p stock-live-market chat \
  -Q \
  -c "Bot Chat" \
  --create-if-missing \
  --source tool \
  --max-turns 6 \
  --run-budget 55 \
  -q "$prompt" 2>"$stderr_file")
status=$?
set -e

if [ "$status" -ne 0 ] || [ -z "$result" ]; then
  if [ "$status" -eq 0 ]; then
    status=1
  fi
  echo "LIVE_DATA_UNAVAILABLE: live-market profile failed or timed out (exit $status)" >&2
  exit "$status"
fi

printf '%s\n' "$result"
