#!/bin/sh
set -eu

mode="${1:-}"
case "$mode" in
  morning|evening|weekly) ;;
  *) echo "usage: run_stock_cycle.sh morning|evening|weekly" >&2; exit 2 ;;
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
  docker compose --env-file "$env_file" -f "$compose_file" --profile candidate "$@"
}

if [ "$mode" = evening ]; then
  compose exec -T stock-assistant python -m stock_assistant ingest-krx \
    --database "$database" --key-file /run/secrets/krx-auth-key --mode daily >/dev/null
fi

if [ "$mode" = evening ] || [ "$mode" = weekly ]; then
  : "${STOCK_DART_BUSINESS_YEAR:?set the approved DART business year}"
  compose exec -T stock-assistant python -m stock_assistant enrich-dart \
    --database "$database" --key-file /run/secrets/dart-api-key \
    --business-year "$STOCK_DART_BUSINESS_YEAR" >/dev/null
fi

if [ "$mode" = weekly ]; then
  compose exec -T stock-assistant python -m stock_assistant evaluate-all-performance \
    --database "$database" --as-of "$(date --iso-8601=seconds)" >/dev/null
fi

compose exec -T stock-assistant python -m stock_assistant enrich-dart-disclosures \
  --database "$database" --key-file /run/secrets/dart-api-key >/dev/null

compose exec -T stock-assistant python -m stock_assistant generate-candidates \
  --database "$database" --limit 5 --format text
