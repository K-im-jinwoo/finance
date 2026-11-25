#!/bin/sh
set -eu

mode=${1:-}
case "$mode" in
  daily|weekly|daily-refresh|refresh-swing) ;;
  *) echo 'usage: stock-swing.sh daily [--once] | weekly' >&2; exit 2 ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)
schedule_env=${STOCK_SCHEDULE_ENV_FILE:-/srv/stock-assistant/schedule.env}
set -a
. "$schedule_env"
set +a

env_file=${STOCK_CANDIDATE_ENV_FILE:-/srv/stock-assistant/candidate.env}
profile=${STOCK_SWING_HERMES_PROFILE:-/home/ubuntu/.hermes/profiles/stock-cio}
python=${STOCK_SWING_PYTHON:-/home/ubuntu/.hermes/hermes-agent/venv/bin/python}
state=${STOCK_STATE_HOST_DIR:-/srv/stock-assistant/state}
database=/var/lib/stock/stock-assistant.sqlite3
research=/var/lib/stock/unified-research-inputs.sqlite3

deliver() {
  PYTHONPATH="$project_root/src" "$python" -B -m stock_assistant.swing_delivery "$1" \
    --profile "$profile" --ledger "$state/swing-telegram.sqlite3"
}

if [ "$mode" = daily-refresh ]; then
  if [ -n "${2:-}" ]; then
    STOCK_UNIFIED_ENABLED=true exec sh "$script_dir/run_stock_cycle.sh" daily-refresh-window "$2"
  fi
  STOCK_UNIFIED_ENABLED=true exec sh "$script_dir/run_stock_cycle.sh" daily-refresh
fi
if [ "$mode" = refresh-swing ]; then
  shift
  if [ -n "${1:-}" ]; then
    case "$1" in
      *[!0-9]*) echo 'Invalid swing cutoff' >&2; exit 2 ;;
    esac
    remaining=$(($1 - $(date +%s)))
    [ "$remaining" -gt 0 ] || exit 124
    set -- timeout -k 1s "${remaining}s"
  fi
  exec docker compose --env-file "$env_file" -f "$project_root/deploy/compose.yaml" \
    -f "$project_root/deploy/compose.news.yaml" --profile candidate exec -T stock-assistant \
    "$@" python -m stock_assistant.research_cli refresh-swing \
    --database "$database" --research-database "$research" \
    --krx-auth-key-file /run/secrets/krx-auth-key \
    --toss-client-id-file /run/secrets/toss-client-id --toss-client-secret-file /run/secrets/toss-client-secret \
    --client-id-file /run/secrets/naver-news-client-id --client-secret-file /run/secrets/naver-news-client-secret \
    --usage-database /var/lib/stock/news-api-usage.sqlite3
fi

deliver check
if [ "$mode" = daily ]; then
  shift
  report=$(PYTHONPATH="$project_root/src" "$python" -B -m stock_assistant.swing_job "$script_dir/stock-swing.sh" "$@")
else
  report=$(docker compose --env-file "$env_file" -f "$project_root/deploy/compose.yaml" \
    --profile candidate exec -T stock-assistant python -m stock_assistant.research_cli evaluate-swing \
    --database "$database" --research-database "$research")
fi
printf '%s\n' "$report" | deliver "$mode"
