#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly BOOTSTRAP_GITHUB_OWNER=ankush29922
readonly BOOTSTRAP_GITHUB_REPO=database
readonly BOOTSTRAP_GITHUB_BRANCH=main
readonly BOOTSTRAP_GITHUB_URL="https://github.com/${BOOTSTRAP_GITHUB_OWNER}/${BOOTSTRAP_GITHUB_REPO}.git"
readonly BOOTSTRAP_GUARD=COMPACTDB_BOOTSTRAP_ACTIVE
readonly INSTALL_ROOT=${COMPACTDB_INSTALL_ROOT:-}

rooted() {
  printf '%s%s\n' "$INSTALL_ROOT" "$1"
}

die() {
  printf 'CompactDB installer: %s\n' "$*" >&2
  exit 1
}

require_root() {
  [[ $EUID -eq 0 ]] || die 'root is required; use the documented sudo command'
}

require_ubuntu_amd64() {
  [[ -r /etc/os-release ]] || die '/etc/os-release is unavailable'
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ ${ID:-} == ubuntu ]] || die 'Ubuntu is required'
  dpkg --compare-versions "${VERSION_ID:-0}" ge 24.04 || die 'Ubuntu 24.04 LTS or newer is required'
  local architecture
  architecture=$(dpkg --print-architecture 2>/dev/null || true)
  [[ "$architecture" == amd64 ]] || die 'Ubuntu amd64 is required'
}

resolve_repository() {
  local source_path source_dir candidate
  source_path=${BASH_SOURCE[0]:-}
  [[ -n "$source_path" && -f "$source_path" ]] || return 1
  source_dir=$(cd -- "$(dirname -- "$source_path")" && pwd)
  candidate=$(cd -- "$source_dir/.." && pwd)
  [[ -d "$candidate/app" && -d "$candidate/deploy" && -d "$candidate/private" && -f "$candidate/requirements.lock" ]] || return 1
  printf '%s\n' "$candidate"
}

bootstrap_mode() {
  [[ ${!BOOTSTRAP_GUARD:-0} != 1 ]] || die 'bootstrap recursion guard triggered'
  printf 'COMPACTDB_INSTALL_MODE=bootstrap\n'
  export DEBIAN_FRONTEND=noninteractive
  local deployment_started
  deployment_started=$(date -u +%FT%TZ)
  apt-get update -qq || die 'failed to refresh Ubuntu package metadata'
  apt-get install -y -qq ca-certificates curl git python3 >/dev/null || die 'failed to install bootstrap prerequisites: ca-certificates curl git python3'

  local temporary checkout status
  temporary=$(mktemp -d /tmp/compactdb-bootstrap.XXXXXX)
  checkout="$temporary/repository"
  COMPACTDB_BOOTSTRAP_TEMPORARY=$temporary
  trap '[[ -z ${COMPACTDB_BOOTSTRAP_TEMPORARY:-} ]] || find "$COMPACTDB_BOOTSTRAP_TEMPORARY" -depth -delete 2>/dev/null || true' EXIT
  git clone --quiet --depth 1 --single-branch --branch "$BOOTSTRAP_GITHUB_BRANCH" "$BOOTSTRAP_GITHUB_URL" "$checkout" || die 'failed to clone the CompactDB repository from GitHub'
  [[ -f "$checkout/deploy/install.sh" ]] || die 'cloned repository does not contain deploy/install.sh'

  set +e
  COMPACTDB_BOOTSTRAP_ACTIVE=1 \
  COMPACTDB_REPOSITORY_MODE=1 \
  COMPACTDB_DEPLOYMENT_START_TIME="$deployment_started" \
    bash "$checkout/deploy/install.sh"
  status=$?
  set -e
  find "$temporary" -depth -delete 2>/dev/null || true
  COMPACTDB_BOOTSTRAP_TEMPORARY=
  trap - EXIT
  (( status == 0 )) || printf 'CompactDB repository installer exited with status %d. Re-run the same command after correcting the reported error.\n' "$status" >&2
  return "$status"
}

installer_phase() {
  local phase=$1 started=$2 state_path package method disk_total disk_free memory_total memory_available swap_total swap_free swap_used
  state_path=$(rooted /var/lib/compactdb/deployment-state.json)
  package=$(rooted /srv/compactdb/CompactDB-Portable)
  method=$(bash -c 'set -u; source "$1" >/dev/null 2>&1; printf "%s" "${DOWNLOAD_METHOD:-auto}"' \
    _ "$(rooted /etc/compactdb/deploy.env)" 2>/dev/null || printf auto)
  read -r disk_total disk_free < <(df -B1 --output=size,avail "$package" | awk 'NR==2{print $1,$2}')
  memory_total=$(awk '/MemTotal:/{print $2*1024}' /proc/meminfo)
  memory_available=$(awk '/MemAvailable:/{print $2*1024}' /proc/meminfo)
  swap_total=$(awk '/SwapTotal:/{print $2*1024}' /proc/meminfo)
  swap_free=$(awk '/SwapFree:/{print $2*1024}' /proc/meminfo)
  swap_used=$((swap_total - swap_free))
  python3 - "$state_path" \
    current_phase="$phase" deployment_start_time="$started" hostname="$(hostname)" \
    configured_download_method="$method" retries=0 disk_total="$disk_total" \
    disk_free="$disk_free" memory_total="$memory_total" \
    memory_available="$memory_available" swap_total="$swap_total" swap_used="$swap_used" \
    bot_service_state=inactive last_error_class=null error_timestamp=null failed_phase=null <<'PY'
import json
import os
import sys
import time

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
except Exception:
    data = {}
for item in sys.argv[2:]:
    key, raw = item.split("=", 1)
    if raw == "null":
        value = None
    else:
        try:
            value = int(raw)
        except ValueError:
            value = raw
    data[key] = value
data["last_state_update"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
temporary = f"{path}.tmp.{os.getpid()}"
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(temporary, "w", encoding="utf-8") as stream:
    json.dump(data, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
  systemctl start --no-block compactdb-notifier.service >/dev/null 2>&1 || true
}

ensure_swap() {
  local target current needed swap_file current_size
  target=$((2 * 1024 * 1024 * 1024))
  current=${COMPACTDB_TEST_SWAP_TOTAL_BYTES:-$(awk '/SwapTotal:/{printf "%.0f",$2*1024}' /proc/meminfo)}
  (( current < target )) || return 0
  needed=$((target - current))
  swap_file=$(rooted /var/lib/compactdb/swapfile)

  if swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -qx "$swap_file"; then
    return 0
  fi
  current_size=$([[ -f "$swap_file" ]] && stat -c '%s' "$swap_file" || printf 0)
  if (( current_size != needed )); then
    rm -f -- "$swap_file"
    fallocate -l "$needed" "$swap_file"
  fi
  chmod 0600 "$swap_file"
  mkswap "$swap_file" >/dev/null
  swapon "$swap_file"
  grep -Fqx "$swap_file none swap sw 0 0" "$(rooted /etc/fstab)" 2>/dev/null || printf '%s none swap sw 0 0\n' "$swap_file" >>"$(rooted /etc/fstab)"
}

ensure_venv() {
  local environment requirements marker marker_file staging
  environment=$1
  requirements=$2
  marker=$(cksum "$requirements" | awk '{print $1 ":" $2}')
  marker_file="${environment}.requirements"
  if [[ -x "$environment/bin/python" && -f "$marker_file" && $(<"$marker_file") == "$marker" ]]; then
    return 0
  fi
  staging="${environment}.new.$$"
  rm -rf -- "$staging"
  python3 -m venv "$staging"
  "$staging/bin/pip" install --disable-pip-version-check --no-input -r "$requirements" >/dev/null
  rm -rf -- "${environment}.previous"
  [[ ! -d "$environment" ]] || mv "$environment" "${environment}.previous"
  mv "$staging" "$environment"
  python3 - "$environment" "$staging" <<'PY'
import os
import pathlib
import sys

environment = pathlib.Path(sys.argv[1])
staging = sys.argv[2].encode()
prefix = b"#!" + staging + b"/"
replacement = b"#!" + str(environment).encode() + b"/"
for launcher in (environment / "bin").iterdir():
    if not launcher.is_file():
        continue
    data = launcher.read_bytes()
    if not data.startswith(prefix):
        continue
    metadata = launcher.stat()
    temporary = launcher.with_name(f".{launcher.name}.{os.getpid()}.tmp")
    temporary.write_bytes(replacement + data[len(prefix):])
    os.chmod(temporary, metadata.st_mode)
    os.chown(temporary, metadata.st_uid, metadata.st_gid)
    os.replace(temporary, launcher)
PY
  printf '%s\n' "$marker" >"$marker_file"
  chown -R root:compactdb "$environment" "$marker_file"
  chmod -R go-w "$environment"
  chmod 0750 "$environment"
  chmod 0640 "$marker_file"
}

install_commands_and_units() {
  local repository=$1
  install -d -m 0755 "$(rooted /usr/local/libexec)"
  install -m 0755 "$repository/deploy/compactdb-telegram-notifier.py" "$(rooted /usr/local/libexec/compactdb-telegram-notifier.py)"
  install -m 0755 "$repository/deploy/compactdb-progress.py" "$(rooted /usr/local/libexec/compactdb-progress.py)"
  install -m 0755 "$repository/deploy/compactdb" "$(rooted /usr/local/bin/compactdb)"
  install -m 0755 "$repository/deploy/compactdb-notify" "$(rooted /usr/local/bin/compactdb-notify)"
  install -m 0755 "$repository/deploy/compactdb-observer" "$(rooted /usr/local/bin/compactdb-observer)"
  install -m 0755 "$repository/deploy/compactdb-deploy" "$(rooted /usr/local/sbin/compactdb-deploy)"
  install -m 0755 "$repository/deploy/compactdb-updater" "$(rooted /usr/local/sbin/compactdb-updater)"
  install -m 0644 "$repository/deploy/compactdb-bot.service" "$(rooted /etc/systemd/system/compactdb-bot.service)"
  install -m 0644 "$repository/deploy/compactdb-deploy.service" "$(rooted /etc/systemd/system/compactdb-deploy.service)"
  install -m 0644 "$repository/deploy/compactdb-update.service" "$(rooted /etc/systemd/system/compactdb-update.service)"
  install -m 0644 "$repository/deploy/compactdb-update.timer" "$(rooted /etc/systemd/system/compactdb-update.timer)"
  install -m 0644 "$repository/deploy/compactdb-notifier.service" "$(rooted /etc/systemd/system/compactdb-notifier.service)"
  install -m 0644 "$repository/deploy/compactdb-notifier.timer" "$(rooted /etc/systemd/system/compactdb-notifier.timer)"
}

configure_systemd() {
  systemctl daemon-reload
  systemctl disable --now compactdb-notify.timer >/dev/null 2>&1 || true
  systemctl enable compactdb-deploy.service compactdb-bot.service compactdb-notifier.timer >/dev/null
  systemctl start compactdb-notifier.timer
  systemctl start --no-block compactdb-notifier.service >/dev/null 2>&1 || true

  if ! bash -c 'set -u; source "$1" >/dev/null 2>&1; [[ -n ${GITHUB_TOKEN:-} ]]' \
      _ "$(rooted /etc/compactdb/update.env)"; then
    systemctl disable --now compactdb-update.timer >/dev/null 2>&1 || true
  else
    systemctl enable --now compactdb-update.timer >/dev/null
  fi

  if [[ $(systemctl show compactdb-deploy.service -p ActiveState --value 2>/dev/null || true) == failed ]]; then
    systemctl reset-failed compactdb-deploy.service
  fi
  systemctl start --no-block compactdb-deploy.service
}

repository_mode() {
  local project_dir repository repair_only deployment_started deploy_environment
  project_dir=$1
  repair_only=${COMPACTDB_REPAIR_ONLY:-0}
  deployment_started=${COMPACTDB_DEPLOYMENT_START_TIME:-$(date -u +%FT%TZ)}
  printf 'COMPACTDB_INSTALL_MODE=repository\n'
  [[ ${COMPACTDB_INSTALL_TEST_STOP_AFTER_MODE:-0} != 1 ]] || return 0

  # shellcheck source=install-lib.sh
  source "$project_dir/deploy/install-lib.sh"
  # shellcheck source=download-lib.sh
  source "$project_dir/deploy/download-lib.sh"

  if ! id compactdb >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/compactdb --shell /usr/sbin/nologin --user-group compactdb
  fi
  install -d -o root -g root -m 0755 "$(rooted /opt/compactdb)" "$(rooted /opt/compactdb/releases)" "$(rooted /usr/local/bin)" "$(rooted /usr/local/sbin)" "$(rooted /usr/local/libexec)" "$(rooted /etc/systemd/system)"
  install -d -o root -g compactdb -m 0750 "$(rooted /srv/compactdb)" "$(rooted /srv/compactdb/CompactDB-Portable)" "$(rooted /srv/compactdb/CompactDB-Portable/database)"
  install -d -o root -g compactdb -m 0750 "$(rooted /var/lib/compactdb)" "$(rooted /var/log/compactdb)"
  install -d -o root -g root -m 0700 "$(rooted /etc/compactdb)"
  install -d -o compactdb -g compactdb -m 0700 "$(rooted /var/lib/compactdb/runtime)" "$(rooted /var/lib/compactdb/duckdb-temp)"

  repository=$(rooted /opt/compactdb/repository)
  compactdb_sync_repository "$project_dir" "$repository"
  compactdb_install_private_configuration "$project_dir/private" "$(rooted /etc/compactdb)"
  install_commands_and_units "$repository"
  systemctl daemon-reload
  systemctl enable compactdb-notifier.timer >/dev/null
  systemctl start compactdb-notifier.timer
  installer_phase BOOTSTRAP_STARTED "$deployment_started"

  export DEBIAN_FRONTEND=noninteractive
  if [[ "$repair_only" != 1 ]]; then
    installer_phase INSTALLING_PREREQUISITES "$deployment_started"
    apt-get update -qq
    apt-get install -y -qq \
      ca-certificates curl git jq python3 python3-pip python3-venv rclone \
      util-linux coreutils tar logrotate >/dev/null
    installer_phase CONFIGURING_SWAP "$deployment_started"
    ensure_swap
    install -d -m 0755 "$(rooted /etc/sysctl.d)"
    printf 'vm.swappiness=10\n' >"$(rooted /etc/sysctl.d/90-compactdb.conf)"
    sysctl -q -p "$(rooted /etc/sysctl.d/90-compactdb.conf)"
  fi

  if [[ "$repair_only" != 1 ]]; then
    installer_phase CREATING_VENV "$deployment_started"
    ensure_venv "$(rooted /opt/compactdb/venv)" "$repository/requirements.lock"
    ensure_venv "$(rooted /opt/compactdb/deploy-venv)" "$repository/deploy/requirements.lock"
  fi
  deploy_environment=$(rooted /opt/compactdb/deploy-venv)
  if [[ -x "$deploy_environment/bin/python" ]] &&
      ! compactdb_repair_gdown_launcher "$deploy_environment"; then
    "$deploy_environment/bin/python" -m pip install --disable-pip-version-check \
      --no-input -r "$repository/deploy/requirements.lock" >/dev/null
    compactdb_repair_gdown_launcher "$deploy_environment" ||
      die 'the permanent gdown launcher could not be repaired'
  fi

  install -d -m 0755 "$(rooted /etc/logrotate.d)"
  install -m 0644 "$repository/deploy/compactdb.logrotate" "$(rooted /etc/logrotate.d/compactdb)"
  configure_systemd

  if [[ ${COMPACTDB_NO_PROGRESS:-0} != 1 ]]; then
    printf 'CompactDB durable deployment is running. Ctrl+C detaches this display only.\n'
    "$(rooted /usr/local/bin/compactdb)" progress
  fi
}

main() {
  require_root
  require_ubuntu_amd64
  local project_dir
  if project_dir=$(resolve_repository); then
    repository_mode "$project_dir"
  else
    bootstrap_mode
  fi
}

main "$@"
