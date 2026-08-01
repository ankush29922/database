#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly GITHUB_OWNER=ankush29922
readonly GITHUB_REPO=database
readonly GITHUB_BRANCH=main
readonly GITHUB_URL="https://github.com/${GITHUB_OWNER}/${GITHUB_REPO}.git"
readonly BOOTSTRAP_GUARD=COMPACTDB_BOOTSTRAP_ACTIVE

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
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git >/dev/null

  local temporary checkout status
  temporary=$(mktemp -d /tmp/compactdb-bootstrap.XXXXXX)
  checkout="$temporary/repository"
  COMPACTDB_BOOTSTRAP_TEMPORARY=$temporary
  trap '[[ -z ${COMPACTDB_BOOTSTRAP_TEMPORARY:-} ]] || find "$COMPACTDB_BOOTSTRAP_TEMPORARY" -depth -delete 2>/dev/null || true' EXIT
  git clone --quiet --depth 1 --single-branch --branch "$GITHUB_BRANCH" "$GITHUB_URL" "$checkout"
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
  return "$status"
}

ensure_swap() {
  local target current needed swap_file current_size
  target=$((2 * 1024 * 1024 * 1024))
  current=$(awk '/SwapTotal:/{printf "%.0f",$2*1024}' /proc/meminfo)
  (( current < target )) || return 0
  needed=$((target - current))
  swap_file=/var/lib/compactdb/swapfile

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
  grep -Fqx "$swap_file none swap sw 0 0" /etc/fstab || printf '%s none swap sw 0 0\n' "$swap_file" >>/etc/fstab
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
  chmod 0640 "$marker_file"
}

install_commands_and_units() {
  local repository=$1
  install -m 0755 "$repository/deploy/compactdb" /usr/local/bin/compactdb
  install -m 0755 "$repository/deploy/compactdb-notify" /usr/local/bin/compactdb-notify
  install -m 0755 "$repository/deploy/compactdb-observer" /usr/local/bin/compactdb-observer
  install -m 0755 "$repository/deploy/compactdb-deploy" /usr/local/sbin/compactdb-deploy
  install -m 0755 "$repository/deploy/compactdb-updater" /usr/local/sbin/compactdb-updater
  install -m 0644 "$repository/deploy/compactdb-bot.service" /etc/systemd/system/compactdb-bot.service
  install -m 0644 "$repository/deploy/compactdb-deploy.service" /etc/systemd/system/compactdb-deploy.service
  install -m 0644 "$repository/deploy/compactdb-update.service" /etc/systemd/system/compactdb-update.service
  install -m 0644 "$repository/deploy/compactdb-update.timer" /etc/systemd/system/compactdb-update.timer
  install -m 0644 "$repository/deploy/compactdb-notify.service" /etc/systemd/system/compactdb-notify.service
  install -m 0644 "$repository/deploy/compactdb-notify.timer" /etc/systemd/system/compactdb-notify.timer
}

configure_systemd() {
  systemctl daemon-reload
  systemctl enable compactdb-deploy.service compactdb-bot.service compactdb-notify.timer >/dev/null
  systemctl start compactdb-notify.timer

  set +u
  # shellcheck disable=SC1091
  source /etc/compactdb/update.env
  set -u
  if [[ -z ${GITHUB_TOKEN:-} ]]; then
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
  install -d -o root -g root -m 0755 /opt/compactdb /opt/compactdb/releases
  install -d -o root -g compactdb -m 0750 /srv/compactdb /srv/compactdb/CompactDB-Portable /srv/compactdb/CompactDB-Portable/database
  install -d -o root -g compactdb -m 0750 /var/lib/compactdb /var/log/compactdb
  install -d -o root -g root -m 0700 /etc/compactdb
  install -d -o compactdb -g compactdb -m 0700 /var/lib/compactdb/runtime /var/lib/compactdb/duckdb-temp

  if [[ "$repair_only" != 1 ]]; then
    ensure_swap
    install -d -m 0755 /etc/sysctl.d
    printf 'vm.swappiness=10\n' >/etc/sysctl.d/90-compactdb.conf
    sysctl -q -p /etc/sysctl.d/90-compactdb.conf
  fi

  repository=/opt/compactdb/repository
  compactdb_sync_repository "$project_dir" "$repository"
  compactdb_install_private_configuration "$project_dir/private" /etc/compactdb
  install_commands_and_units "$repository"

  if [[ "$repair_only" != 1 ]]; then
    ensure_venv /opt/compactdb/venv "$repository/requirements.lock"
    ensure_venv /opt/compactdb/deploy-venv "$repository/deploy/requirements.lock"
  fi

  install -m 0644 "$repository/deploy/compactdb.logrotate" /etc/logrotate.d/compactdb
  configure_systemd

  if [[ ${COMPACTDB_NO_PROGRESS:-0} != 1 ]]; then
    printf 'CompactDB durable deployment is running. Ctrl+C detaches this display only.\n'
    /usr/local/bin/compactdb progress
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
