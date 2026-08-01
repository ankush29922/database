#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-installation.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
mock_bin="$temporary/bin"
fake_root="$temporary/root"
mock_state="$temporary/state"
mock_log="$temporary/calls.log"
install -d "$mock_bin" "$fake_root" "$mock_state"

cat >"$mock_bin/apt-get" <<'MOCK'
#!/usr/bin/env bash
printf 'apt-get %s\n' "$*" >>"$MOCK_LOG"
MOCK
cat >"$mock_bin/id" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == compactdb && -f "$MOCK_STATE/user_exists" ]]
MOCK
cat >"$mock_bin/useradd" <<'MOCK'
#!/usr/bin/env bash
touch "$MOCK_STATE/user_exists"
printf 'useradd\n' >>"$MOCK_LOG"
MOCK
cat >"$mock_bin/python3" <<'MOCK'
#!/usr/bin/env bash
if [[ ${1:-} == - ]]; then
  exec /usr/bin/python3 "$@"
fi
[[ ${1:-} == -m && ${2:-} == venv && -n ${3:-} ]] || exit 2
environment=$3
install -d "$environment/bin"
cat >"$environment/bin/pip" <<'PIP'
#!/usr/bin/env bash
printf 'pip-install\n' >>"$MOCK_LOG"
environment=$(cd -- "$(dirname -- "$0")/.." && pwd)
{
  printf '#!%s/bin/python\n' "$environment"
  printf '%s\n' 'from gdown.cli import main'
} >"$environment/bin/gdown"
chmod 0755 "$environment/bin/gdown"
PIP
cat >"$environment/bin/python" <<'PYTHON'
#!/usr/bin/env bash
printf 'permanent-python %s\n' "$*" >>"$MOCK_LOG"
exit 0
PYTHON
chmod 0755 "$environment/bin/pip" "$environment/bin/python"
printf 'venv-create\n' >>"$MOCK_LOG"
MOCK
cat >"$mock_bin/systemctl" <<'MOCK'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >>"$MOCK_LOG"
case " $* " in
  *' show compactdb-deploy.service '*)
    if [[ -f "$MOCK_STATE/deploy_active" ]]; then printf 'active\n'; else printf 'inactive\n'; fi
    ;;
  *' start --no-block compactdb-deploy.service '*)
    if [[ ! -f "$MOCK_STATE/deploy_active" ]]; then
      touch "$MOCK_STATE/deploy_active"
      printf 'deploy-launch\n' >>"$MOCK_LOG"
    fi
    ;;
esac
MOCK
cat >"$mock_bin/sysctl" <<'MOCK'
#!/usr/bin/env bash
printf 'sysctl\n' >>"$MOCK_LOG"
MOCK
cat >"$mock_bin/swapon" <<'MOCK'
#!/usr/bin/env bash
if [[ ${1:-} == --show* ]]; then
  [[ ! -f "$MOCK_STATE/swap_active" ]] || printf '%s\n' "$MOCK_FAKE_ROOT/var/lib/compactdb/swapfile"
else
  touch "$MOCK_STATE/swap_active"
  printf 'swapon\n' >>"$MOCK_LOG"
fi
MOCK
cat >"$mock_bin/fallocate" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == -l && -n ${2:-} && -n ${3:-} ]] || exit 2
/usr/bin/truncate -s "$2" "$3"
printf 'fallocate\n' >>"$MOCK_LOG"
MOCK
cat >"$mock_bin/mkswap" <<'MOCK'
#!/usr/bin/env bash
printf 'mkswap\n' >>"$MOCK_LOG"
MOCK
chmod 0755 "$mock_bin"/*

run_install() {
  PATH="$mock_bin:/usr/bin:/bin" \
  MOCK_LOG="$mock_log" \
  MOCK_STATE="$mock_state" \
  MOCK_FAKE_ROOT="$fake_root" \
  COMPACTDB_INSTALL_ROOT="$fake_root" \
  COMPACTDB_TEST_SWAP_TOTAL_BYTES=0 \
  COMPACTDB_NO_PROGRESS=1 \
    bash "$repository/deploy/install.sh" >/dev/null
}

run_install
[[ -f "$mock_state/user_exists" && -f "$mock_state/swap_active" && -f "$mock_state/deploy_active" ]]
for directory in opt/compactdb srv/compactdb etc/compactdb var/lib/compactdb var/log/compactdb; do
  [[ -d "$fake_root/$directory" ]]
done
for command in usr/local/bin/compactdb usr/local/sbin/compactdb-deploy; do
  [[ -x "$fake_root/$command" ]]
done
[[ -x "$fake_root/usr/local/libexec/compactdb-telegram-notifier.py" ]]
[[ -x "$fake_root/usr/local/libexec/compactdb-progress.py" ]]
for unit in compactdb-bot.service compactdb-deploy.service compactdb-notifier.service compactdb-notifier.timer; do
  [[ -f "$fake_root/etc/systemd/system/$unit" ]]
done
for configuration in bot.env deploy.env paths.env rclone.conf update.env; do
  [[ -f "$fake_root/etc/compactdb/$configuration" ]]
  [[ $(stat -c '%a:%U:%G' "$fake_root/etc/compactdb/$configuration") == 600:root:root ]]
done
[[ $(stat -c '%a:%U:%G' "$fake_root/opt/compactdb/venv") == 750:root:compactdb ]]
permanent_deploy_venv="$fake_root/opt/compactdb/deploy-venv"
permanent_gdown="$permanent_deploy_venv/bin/gdown"
[[ -x "$permanent_deploy_venv/bin/python" && -x "$permanent_gdown" ]]
[[ $(head -n 1 "$permanent_gdown") == "#!$permanent_deploy_venv/bin/python" ]]
[[ -z $(find "$fake_root/opt/compactdb" -maxdepth 1 -type d -name 'deploy-venv.new.*' -print -quit) ]]
MOCK_LOG="$mock_log" "$permanent_gdown" --version
grep -Fq "permanent-python $permanent_gdown --version" "$mock_log"
[[ $(grep -c '^useradd$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^fallocate$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^deploy-launch$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^pip-install$' "$mock_log") -eq 2 ]]
notifier_start_line=$(grep -n -m1 '^systemctl start --no-block compactdb-notifier.service$' "$mock_log" | cut -d: -f1)
deploy_launch_line=$(grep -n -m1 '^deploy-launch$' "$mock_log" | cut -d: -f1)
(( notifier_start_line < deploy_launch_line ))
printf 'MOCK_FIRST_INSTALL=PASS\n'

chmod 0644 "$fake_root/etc/compactdb/bot.env"
find "$fake_root/etc/compactdb" -maxdepth 1 -type f -name rclone.conf -delete
stale_python="$fake_root/opt/compactdb/deploy-venv.new.deleted/bin/python"
{
  printf '#!%s\n' "$stale_python"
  printf '%s\n' 'raise SystemExit(99)'
} >"$permanent_gdown"
chmod 0755 "$permanent_gdown"
[[ ! -e "$stale_python" ]]
run_install
[[ $(stat -c '%a:%U:%G' "$fake_root/etc/compactdb/bot.env") == 600:root:root ]]
[[ $(stat -c '%a:%U:%G' "$fake_root/etc/compactdb/rclone.conf") == 600:root:root ]]
[[ $(grep -c '^useradd$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^fallocate$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^deploy-launch$' "$mock_log") -eq 1 ]]
[[ $(grep -c '^pip-install$' "$mock_log") -eq 2 ]]
[[ -x "$permanent_gdown" ]]
[[ $(head -n 1 "$permanent_gdown") == "#!$permanent_deploy_venv/bin/python" ]]
MOCK_LOG="$mock_log" "$permanent_gdown" --version
printf 'MOCK_REPEATED_INSTALL=PASS\n'
printf 'MOCK_PERMANENT_GDOWN_AFTER_CLEANUP=PASS\n'
printf 'MOCK_STALE_GDOWN_LAUNCHER_REPAIRED=PASS\n'
