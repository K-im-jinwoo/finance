#!/bin/sh
set -eu

local_hhmm=$(TZ=Asia/Seoul date +%H%M)
if [ "$local_hhmm" -lt 0900 ] || [ "$local_hhmm" -gt 1530 ]; then
  exit 0
fi

if [ -f /srv/stock-assistant/schedule.env ]; then
  set -a
  . /srv/stock-assistant/schedule.env
  set +a
fi

exec sh /srv/stock-assistant/current/deploy/jobs/run_intraday_watch.sh "" alerts-only
