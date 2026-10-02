#!/bin/sh
set -eu

schedule_env="${STOCK_SCHEDULE_ENV_FILE:-/srv/stock-assistant/schedule.env}"
if [ ! -r "$schedule_env" ]; then
  echo "stock schedule env file is missing" >&2
  exit 2
fi

set -a
. "$schedule_env"
set +a

exec sh /srv/stock-assistant/current/deploy/jobs/run_stock_cycle.sh evening
