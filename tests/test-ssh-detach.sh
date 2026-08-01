#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-detach.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
mock_bin="$temporary/bin"
install -d "$mock_bin"

cat >"$mock_bin/jq" <<'MOCK'
#!/usr/bin/env bash
key=
while (($#)); do
  if [[ $1 == --arg && ${2:-} == key ]]; then key=${3:-}; shift 3; else shift; fi
done
case "$key" in
  current_phase) printf 'DOWNLOADING\n' ;;
  DOWNLOAD_STATUS) printf 'IN_PROGRESS\n' ;;
  percentage) printf '1\n' ;;
  *) printf '0\n' ;;
esac
MOCK
cat >"$mock_bin/observer" <<'MOCK'
#!/usr/bin/env bash
printf 'observer\n' >>"$MOCK_LOG"
exit 0
MOCK
cat >"$mock_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf 'UNEXPECTED_SYSTEMCTL_ACTION=%s\n' "$*" >>"$MOCK_LOG"
exit 1
MOCK
chmod 0755 "$mock_bin"/*
touch "$temporary/state.json" "$temporary/actions.log"

setsid env \
  PATH="$mock_bin:/usr/bin:/bin" \
  MOCK_LOG="$temporary/actions.log" \
  COMPACTDB_STATE_PATH="$temporary/state.json" \
  COMPACTDB_OBSERVER_COMMAND="$mock_bin/observer" \
  TERM=dumb \
    bash "$repository/deploy/compactdb" progress >/dev/null 2>&1 &
progress_pid=$!
sleep 0.2
kill -HUP -- "-$progress_pid"
set +e
wait "$progress_pid" 2>/dev/null
detach_status=$?
set -e
(( detach_status != 0 ))
grep -Fqx 'observer' "$temporary/actions.log"
! grep -Fq 'UNEXPECTED_SYSTEMCTL_ACTION=' "$temporary/actions.log"
printf 'MOCK_SSH_DETACH=PASS\n'

: >"$temporary/actions.log"
set +e
timeout --signal=INT --kill-after=1 0.2 \
  env PATH="$mock_bin:/usr/bin:/bin" \
      MOCK_LOG="$temporary/actions.log" \
      COMPACTDB_STATE_PATH="$temporary/state.json" \
      COMPACTDB_OBSERVER_COMMAND="$mock_bin/observer" \
      TERM=dumb \
    bash "$repository/deploy/compactdb" progress >/dev/null 2>&1
interrupt_status=$?
set -e
(( interrupt_status != 0 ))
grep -Fqx 'observer' "$temporary/actions.log"
! grep -Fq 'UNEXPECTED_SYSTEMCTL_ACTION=' "$temporary/actions.log"
printf 'MOCK_CTRL_C_DETACH=PASS\n'
