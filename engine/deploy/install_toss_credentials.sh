#!/usr/bin/env bash
set -euo pipefail

target_dir=/srv/stock-assistant
client_id_file="$target_dir/toss-client-id"
client_secret_file="$target_dir/toss-client-secret"
tmp_id=$(mktemp "$target_dir/.toss-client-id.XXXXXX")
tmp_secret=$(mktemp "$target_dir/.toss-client-secret.XXXXXX")

cleanup() {
  rm -f -- "$tmp_id" "$tmp_secret"
  unset toss_client_id toss_client_secret
}
trap cleanup EXIT
umask 077

read -rsp "Toss client_id: " toss_client_id
printf '\n'
read -rsp "Toss client_secret: " toss_client_secret
printf '\n'

if [[ -z "$toss_client_id" || -z "$toss_client_secret" ]]; then
  echo "client_id and client_secret are required" >&2
  exit 1
fi

printf '%s' "$toss_client_id" >"$tmp_id"
printf '%s' "$toss_client_secret" >"$tmp_secret"
chmod 600 "$tmp_id" "$tmp_secret"
mv -f -- "$tmp_id" "$client_id_file"
mv -f -- "$tmp_secret" "$client_secret_file"
echo "Toss credentials installed with mode 600. Values were not printed."
