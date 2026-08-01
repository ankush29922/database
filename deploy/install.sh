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
  apt-get update -qq || die 'failed to refresh Ubuntu package metadata'
  apt-get install -y -qq ca-certificates curl git >/dev/null || die 'failed to install bootstrap prerequisites: ca-certificates curl git'

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
    bash "$checkout/deploy/install.sh"
  status=$?
  set -e
  find "$temporary" -depth -delete 2>/dev/null || true
  COMPACTDB_BOOTSTRAP_TEMPORARY=
  trap - EXIT
  (( status == 0 )) || printf 'CompactDB repository installer exited with status %d. Re-run the same command after correcting the reported error.\n' "$status" >&2
  return "$status"
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
  printf '%s\n' "$marker" >"$marker_file"
  chown -R root:compactdb "$environment" "$marker_file"
  chmod -R go-w "$environment"
  chmod 0750 "$environment"
  chmod 0640 "$marker_file"
}

install_commands_and_units() {
  local repository=$1
  install -m 0755 "$repository/deploy/compactdb" "$(rooted /usr/local/bin/compactdb)"
  install -m 0755 "$repository/deploy/compactdb-notify" "$(rooted /usr/local/bin/compactdb-notify)"
  install -m 0755 "$repository/deploy/compactdb-observer" "$(rooted /usr/local/bin/compactdb-observer)"
  install -m 0755 "$repository/deploy/compactdb-deploy" "$(rooted /usr/local/sbin/compactdb-deploy)"
  install -m 0755 "$repository/deploy/compactdb-updater" "$(rooted /usr/local/sbin/compactdb-updater)"
  install -m 0644 "$repository/deploy/compactdb-bot.service" "$(rooted /etc/systemd/system/compactdb-bot.service)"
  install -m 0644 "$repository/deploy/compactdb-deploy.service" "$(rooted /etc/systemd/system/compactdb-deploy.service)"
  install -m 0644 "$repository/deploy/compactdb-update.service" "$(rooted /etc/systemd/system/compactdb-update.service)"
  install -m 0644 "$repository/deploy/compactdb-update.timer" "$(rooted /etc/systemd/system/compactdb-update.timer)"
  install -m 0644 "$repository/deploy/compactdb-notify.service" "$(rooted /etc/systemd/system/compactdb-notify.service)"
  install -m 0644 "$repository/deploy/compactdb-notify.timer" "$(rooted /etc/systemd/system/compactdb-notify.timer)"
}

configure_systemd() {
  systemctl daemon-reload
  systemctl enable compactdb-deploy.service compactdb-bot.service compactdb-notify.timer >/dev/null
  systemctl start compactdb-notify.timer

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
  local project_dir repository repair_only
  project_dir=$1
  repair_only=${COMPACTDB_REPAIR_ONLY:-0}
  printf 'COMPACTDB_INSTALL_MODE=repository\n'
  [[ ${COMPACTDB_INSTALL_TEST_STOP_AFTER_MODE:-0} != 1 ]] || return 0

  # shellcheck source=install-lib.sh
  source "$project_dir/deploy/install-lib.sh"

  export DEBIAN_FRONTEND=noninteractive
  if [[ "$repair_only" != 1 ]]; then
    apt-get update -qq
    apt-get install -y -qq \
      ca-certificates curl git jq python3 python3-pip python3-venv rclone \
      util-linux coreutils tar logrotate >/dev/null
  fi

  if ! id compactdb >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/compactdb --shell /usr/sbin/nologin --user-group compactdb
  fi
  install -d -o root -g root -m 0755 "$(rooted /opt/compactdb)" "$(rooted /opt/compactdb/releases)" "$(rooted /usr/local/bin)" "$(rooted /usr/local/sbin)" "$(rooted /etc/systemd/system)"
  install -d -o root -g compactdb -m 0750 "$(rooted /srv/compactdb)" "$(rooted /srv/compactdb/CompactDB-Portable)" "$(rooted /srv/compactdb/CompactDB-Portable/database)"
  install -d -o root -g compactdb -m 0750 "$(rooted /var/lib/compactdb)" "$(rooted /var/log/compactdb)"
  install -d -o root -g root -m 0700 "$(rooted /etc/compactdb)"
  install -d -o compactdb -g compactdb -m 0700 "$(rooted /var/lib/compactdb/runtime)" "$(rooted /var/lib/compactdb/duckdb-temp)"

  if [[ "$repair_only" != 1 ]]; then
    ensure_swap
    install -d -m 0755 "$(rooted /etc/sysctl.d)"
    printf 'vm.swappiness=10\n' >"$(rooted /etc/sysctl.d/90-compactdb.conf)"
    sysctl -q -p "$(rooted /etc/sysctl.d/90-compactdb.conf)"
  fi

  repository=$(rooted /opt/compactdb/repository)
  compactdb_sync_repository "$project_dir" "$repository"
  compactdb_install_private_configuration "$project_dir/private" "$(rooted /etc/compactdb)"
  install_commands_and_units "$repository"

  if [[ "$repair_only" != 1 ]]; then
    ensure_venv "$(rooted /opt/compactdb/venv)" "$repository/requirements.lock"
    ensure_venv "$(rooted /opt/compactdb/deploy-venv)" "$repository/deploy/requirements.lock"
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
