#!/bin/sh
set -eu

stage=SETUP
trap 'code=$?; if [ "$code" -ne 0 ]; then printf "STOCK_CYCLE status=FAILED stage=%s exit=%s\n" "$stage" "$code" >&2; fi' 0

mode="${1:-}"
morning_cutoff=""
case "$mode" in
  morning-window|daily-refresh-window)
    mode=${mode%-window}
    morning_cutoff="${2:-}"
    case "$morning_cutoff" in
      ''|*[!0-9]*) echo "Invalid morning daily cutoff" >&2; exit 2 ;;
    esac
    ;;
  morning|evening|weekly|daily-refresh) ;;
  *) echo "usage: run_stock_cycle.sh morning|evening|weekly|daily-refresh|(morning-window|daily-refresh-window) CUTOFF_EPOCH" >&2; exit 2 ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
compose_file="$project_root/deploy/compose.yaml"
if [ -n "${STOCK_CANDIDATE_ENV_FILE:-}" ]; then
  env_file="$STOCK_CANDIDATE_ENV_FILE"
elif [ -f /srv/stock-assistant/candidate.env ]; then
  env_file=/srv/stock-assistant/candidate.env
else
  env_file="$project_root/deploy/candidate.env"
fi
database=/var/lib/stock/stock-assistant.sqlite3

if [ ! -f "$env_file" ]; then
  echo "candidate env file is missing" >&2
  exit 2
fi

compose() {
  if [ "${STOCK_NEWS_ENABLED:-false}" = true ]; then
    docker compose --env-file "$env_file" -f "$compose_file" \
      -f "$project_root/deploy/compose.news.yaml" --profile candidate "$@"
  else
    docker compose --env-file "$env_file" -f "$compose_file" --profile candidate "$@"
  fi
}

case "${STOCK_NEWS_ENABLED:-false}" in
  true|false) ;;
  *) echo "STOCK_NEWS_ENABLED must be true or false" >&2; exit 2 ;;
esac

case "${STOCK_CANDIDATE_CHARTS:-false}" in
  true|false) ;;
  *) echo "STOCK_CANDIDATE_CHARTS must be true or false" >&2; exit 2 ;;
esac

case "${STOCK_ACTIONABLE_ENABLED:-false}" in
  true|false) ;;
  *) echo "STOCK_ACTIONABLE_ENABLED must be true or false" >&2; exit 2 ;;
esac

case "${STOCK_COMMITTEE_BRIEF:-false}" in
  true|false) ;;
  *) echo "STOCK_COMMITTEE_BRIEF must be true or false" >&2; exit 2 ;;
esac

emit_candidate_charts() {
  chart_report_id=$(printf '%s\n' "$1" | sed -n 's/^보고서: //p')
  chart_python=${STOCK_CHART_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}
  chart_state=${STOCK_STATE_HOST_DIR:-/srv/stock-assistant/state}
  chart_home=${HERMES_HOME:-/home/ubuntu/.hermes/profiles/stock-cio}
  PYTHONPATH="$project_root/src" "$chart_python" -m stock_assistant.candidate_charts \
    --database "$chart_state/stock-assistant.sqlite3" \
    --research-database "$chart_state/unified-research-inputs.sqlite3" \
    --report-id "$chart_report_id" --format telegram \
    --output-dir "${STOCK_CHART_OUTPUT_DIR:-$chart_home/cache/candidate-charts}"
}

emit_committee_brief() {
  brief_report_id=$(printf '%s\n' "$1" | sed -n 's/^보고서: //p')
  brief_python=${STOCK_COMMITTEE_PYTHON:-${STOCK_CHART_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}}
  brief_state=${STOCK_STATE_HOST_DIR:-/srv/stock-assistant/state}
  brief_home=${HERMES_HOME:-/home/ubuntu/.hermes/profiles/stock-cio}
  brief_script=${STOCK_COMMITTEE_SCRIPT:-$brief_home/scripts/stock-committee-brief.py}
  brief_chart_dir=${2:-}
  if [ -n "$brief_chart_dir" ] && [ -d "$brief_chart_dir" ]; then
    "$brief_python" "$brief_script" \
      --database "$brief_state/stock-assistant.sqlite3" \
      --research-database "$brief_state/unified-research-inputs.sqlite3" \
      --report-id "$brief_report_id" --chart-dir "$brief_chart_dir" --format telegram
  else
    "$brief_python" "$brief_script" \
      --database "$brief_state/stock-assistant.sqlite3" \
      --research-database "$brief_state/unified-research-inputs.sqlite3" \
      --report-id "$brief_report_id" --format telegram
  fi
}

stage=DAILY_REFRESH
daily_args=""
if [ "${STOCK_UNIFIED_ENABLED:-false}" = true ]; then
  daily_args="--include-completed-today"
  if [ -z "$morning_cutoff" ]; then
    daily_args="$daily_args --allow-unpublished-latest"
  fi
fi

refresh_daily() {
  compose exec -T stock-assistant "$@" python -m stock_assistant refresh-daily \
    --database "$database" --key-file /run/secrets/krx-auth-key \
    --client-id-file /run/secrets/toss-client-id --client-secret-file /run/secrets/toss-client-secret $daily_args
}

if [ -n "$morning_cutoff" ]; then
  remaining_seconds=$((morning_cutoff - $(date +%s)))
  [ "$remaining_seconds" -gt 0 ] || exit 124
  refresh_daily timeout -k 1s "${remaining_seconds}s" >&2
  [ "$(date +%s)" -lt "$morning_cutoff" ] || exit 124
else
  refresh_daily >&2
fi

if [ "$mode" = daily-refresh ]; then
  printf 'STOCK_CYCLE status=READY mode=%s stage=DAILY_REFRESH\n' "$mode" >&2
  exit 0
fi

if [ "${STOCK_UNIFIED_ENABLED:-false}" = true ]; then
  stage=UNIFIED_RESEARCH
  actionable_args=""
  if [ "${STOCK_ACTIONABLE_ENABLED:-false}" = true ]; then
    actionable_args="--actionable"
  fi
  unified_output=$(compose exec -T stock-assistant python -m stock_assistant.research_cli refresh-unified \
    --database "$database" --research-database /var/lib/stock/unified-research-inputs.sqlite3 \
    --api-key-file /run/secrets/dart-api-key --batch-size 25 \
    --client-id-file /run/secrets/naver-news-client-id --client-secret-file /run/secrets/naver-news-client-secret \
    --usage-database /var/lib/stock/news-api-usage.sqlite3 \
    --toss-client-id-file /run/secrets/toss-client-id --toss-client-secret-file /run/secrets/toss-client-secret \
    $actionable_args --format text)
  if [ "$mode" = weekly ]; then
    stage=PERFORMANCE
    compose exec -T stock-assistant python -m stock_assistant evaluate-all-performance \
      --database "$database" --research-database /var/lib/stock/unified-research-inputs.sqlite3 \
      --as-of "$(date +%Y-%m-%dT%H:%M:%S.%6N%:z)" >/dev/null
  fi
  printf '[장기]\n%s\n' "$unified_output"
  chart_output=""
  chart_failed=false
  rendered_chart_dir=""
  if [ "${STOCK_CANDIDATE_CHARTS:-false}" = true ]; then
    stage=CANDIDATE_CHARTS
    chart_report_id=$(printf '%s\n' "$unified_output" | sed -n 's/^보고서: //p')
    chart_home=${HERMES_HOME:-/home/ubuntu/.hermes/profiles/stock-cio}
    chart_base_dir=${STOCK_CHART_OUTPUT_DIR:-$chart_home/cache/candidate-charts}
    if chart_output=$(emit_candidate_charts "$unified_output"); then
      rendered_chart_dir="$chart_base_dir/$chart_report_id"
    else
      chart_failed=true
    fi
  fi
  if [ "${STOCK_COMMITTEE_BRIEF:-false}" = true ]; then
    stage=COMMITTEE_BRIEF
    if ! emit_committee_brief "$unified_output" "$rendered_chart_dir"; then
      printf '투자위원회 요약 실패 · 후보 보고서와 판정은 유지합니다.\n'
    fi
  fi
  if [ "${STOCK_CANDIDATE_CHARTS:-false}" = true ]; then
    if [ "$chart_failed" = true ]; then
      printf '기술적 차트 첨부 실패 · 후보 보고서와 판정은 유지합니다.\n'
    elif [ -n "$chart_output" ]; then
      printf '%s\n' "$chart_output"
    fi
  fi
  printf 'STOCK_CYCLE status=READY mode=%s stage=UNIFIED_RESEARCH\n' "$mode" >&2
  exit 0
fi

news_report_args=""
news_enrichment_args=""
if [ "${STOCK_NEWS_ENABLED:-false}" = true ]; then
  stage=NEWS_COLLECTION
  if compose exec -T stock-assistant python -m stock_assistant discover-news \
    --database "$database" --client-id-file /run/secrets/naver-news-client-id \
    --client-secret-file /run/secrets/naver-news-client-secret >/dev/null; then
    news_report_args="--include-news"
    news_enrichment_args="--include-news"
  else
    news_report_args="--include-news --news-fetch-failed"
    echo "News collection unavailable; continuing with existing research inputs." >&2
  fi
fi

if [ "$mode" = evening ] || [ "$mode" = weekly ] || [ -n "$news_enrichment_args" ]; then
  stage=DART_FINANCIALS
  : "${STOCK_DART_BUSINESS_YEAR:?set the approved DART business year}"
  compose exec -T stock-assistant python -m stock_assistant enrich-dart \
    --database "$database" --key-file /run/secrets/dart-api-key \
    --business-year "$STOCK_DART_BUSINESS_YEAR" $news_enrichment_args >/dev/null
fi

if [ "$mode" = weekly ]; then
  stage=PERFORMANCE
  compose exec -T stock-assistant python -m stock_assistant evaluate-all-performance \
    --database "$database" --as-of "$(date --iso-8601=seconds)" >/dev/null
fi

stage=DART_DISCLOSURES
compose exec -T stock-assistant python -m stock_assistant enrich-dart-disclosures \
  --database "$database" --key-file /run/secrets/dart-api-key $news_enrichment_args >/dev/null

stage=REPORT_GENERATION
candidate_output=$(compose exec -T stock-assistant python -m stock_assistant generate-candidates \
  --database "$database" --limit 5 --format text $news_report_args)
printf '[장기]\n%s\n' "$candidate_output"
printf 'STOCK_CYCLE status=READY mode=%s stage=REPORT_GENERATION\n' "$mode" >&2
