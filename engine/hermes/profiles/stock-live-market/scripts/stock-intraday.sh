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

exec sh /srv/stock-assistant/current/deploy/jobs/stock-intraday.sh "$symbols"
