#!/bin/sh
set -eu

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

symbols="${1:-}"
mode="${2:-full}"
case "$symbols" in
  "") explicit_args="" ;;
  *[!0-9A-Z,]*) echo "symbols must be comma-separated six-character codes" >&2; exit 2 ;;
  *) explicit_args="--symbols $symbols" ;;
esac

case "$mode" in
  full) alert_args="" ;;
  alerts-only) alert_args="--alerts-only" ;;
  *) echo "mode must be full or alerts-only" >&2; exit 2 ;;
esac

# shellcheck disable=SC2086
exec docker compose --env-file "$env_file" -f "$compose_file" --profile candidate \
  exec -T stock-assistant python -m stock_assistant refresh-intraday \
  --database "$database" \
  --client-id-file /run/secrets/toss-client-id \
  --client-secret-file /run/secrets/toss-client-secret \
  --candidate-limit 5 --candle-count 30 $explicit_args $alert_args
