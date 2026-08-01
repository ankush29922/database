#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-observer.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
mock_bin="$temporary/bin"
install -d "$mock_bin"

cat >"$mock_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
case "$MOCK_SYSTEMD_STATE" in
  active|active_no_log)
    printf 'ActiveState=active\nSubState=running\nMainPID=4242\n'
    ;;
  failed)
    printf 'ActiveState=failed\nSubState=failed\nMainPID=0\n'
    ;;
  inactive)
    printf 'ActiveState=inactive\nSubState=dead\nMainPID=0\n'
    ;;
  *) exit 2 ;;
esac
MOCK
cat >"$mock_bin/journalctl" <<'MOCK'
#!/usr/bin/env bash
[[ "$MOCK_SYSTEMD_STATE" == active ]] || exit 1
printf 'Application started\n'
MOCK
chmod 0755 "$mock_bin/systemctl" "$mock_bin/journalctl"

active_output=$(PATH="$mock_bin:/usr/bin:/bin" MOCK_SYSTEMD_STATE=active bash "$repository/deploy/compactdb-observer")
grep -Fqx 'ACTIVE_STATE=active' <<<"$active_output"
grep -Fqx 'SUB_STATE=running' <<<"$active_output"
grep -Fqx 'MAIN_PID=4242' <<<"$active_output"
grep -Fqx 'TELEGRAM_APPLICATION_STARTED=yes' <<<"$active_output"

for state in failed inactive active_no_log; do
  if PATH="$mock_bin:/usr/bin:/bin" MOCK_SYSTEMD_STATE="$state" \
      bash "$repository/deploy/compactdb-observer" >/dev/null; then
    printf '%s state was incorrectly healthy\n' "$state" >&2
    exit 1
  fi
done
! grep -Eq '(^|[^[:alnum:]_])pgrep([^[:alnum:]_]|$)' "$repository/deploy/compactdb-observer"
printf 'MOCK_OBSERVER=PASS\n'
