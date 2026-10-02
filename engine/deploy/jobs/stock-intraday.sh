#!/bin/sh
set -eu

if [ -f /srv/stock-assistant/schedule.env ]; then
  set -a
  . /srv/stock-assistant/schedule.env
  set +a
fi

exec sh /srv/stock-assistant/current/deploy/jobs/run_intraday_watch.sh "${1:-}"
