#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

GITHUB_OWNER=REPLACE_OWNER
GITHUB_REPO=REPLACE_REPO
GITHUB_BRANCH=main

if [[ $EUID -ne 0 ]]; then
  echo 'Run this installer as root (for example, pipe it to sudo bash).' >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl jq python3 python3-venv rclone util-linux coreutils tar logrotate >/dev/null

if ! id compactdb >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/compactdb --shell /usr/sbin/nologin --user-group compactdb
fi
install -d -o root -g root -m 0755 /opt/compactdb /opt/compactdb/releases
install -d -o root -g compactdb -m 0750 /srv/compactdb /srv/compactdb/CompactDB-Portable
install -d -o root -g compactdb -m 0750 /var/lib/compactdb /var/log/compactdb
install -d -o root -g root -m 0700 /etc/compactdb
install -d -o compactdb -g compactdb -m 0700 /var/lib/compactdb/runtime /var/lib/compactdb/duckdb-temp

target_swap=$((2 * 1024 * 1024 * 1024))
current_swap=$(awk '/SwapTotal:/{printf "%.0f",$2*1024}' /proc/meminfo)
if (( current_swap > target_swap )); then
  echo "Existing swap exceeds the required exact 2 GiB total; refusing to disable or shrink it." >&2
  exit 1
fi
if (( current_swap < target_swap )); then
  need=$((target_swap-current_swap))
  if swapon --show=NAME --noheadings | awk '{$1=$1};1' | grep -qx /swapfile; then
    echo 'Active /swapfile is smaller than required; refusing an unsafe live replacement.' >&2
    exit 1
  fi
  [[ ! -e /swapfile ]] || rm -f /swapfile
  fallocate -l "$need" /swapfile
  chmod 0600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -Eq '^/swapfile[[:space:]]+none[[:space:]]+swap([[:space:]]|$)' /etc/fstab || printf '/swapfile none swap sw 0 0\n' >>/etc/fstab
fi
install -d -m 0755 /etc/sysctl.d
printf 'vm.swappiness=10\n' >/etc/sysctl.d/90-compactdb.conf
sysctl -q -p /etc/sysctl.d/90-compactdb.conf

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/nonexistent}")" 2>/dev/null && pwd || true)
project_dir=$(cd "$script_dir/.." 2>/dev/null && pwd || true)
temporary=
if [[ ! -f "$project_dir/requirements.lock" || ! -d "$project_dir/app" ]]; then
  if [[ "$GITHUB_OWNER" == REPLACE_OWNER || "$GITHUB_REPO" == REPLACE_REPO ]]; then
    echo 'Replace REPLACE_OWNER and REPLACE_REPO in deploy/install.sh before public one-command use.' >&2
    exit 1
  fi
  temporary=$(mktemp -d /tmp/compactdb-installer.XXXXXX)
  trap '[[ -n ${temporary:-} ]] && rm -rf "$temporary"' EXIT
  curl -fsSL --retry 5 "https://codeload.github.com/$GITHUB_OWNER/$GITHUB_REPO/tar.gz/refs/heads/$GITHUB_BRANCH" -o "$temporary/source.tgz"
  tar -xzf "$temporary/source.tgz" -C "$temporary"
  project_dir=$(find "$temporary" -mindepth 1 -maxdepth 1 -type d | head -n1)
fi

rm -rf /opt/compactdb/repository.new
install -d -m 0755 /opt/compactdb/repository.new
cp -a "$project_dir/app" "$project_dir/deploy" "$project_dir/requirements.lock" /opt/compactdb/repository.new/
chown -R root:root /opt/compactdb/repository.new
chmod -R go-w /opt/compactdb/repository.new
[[ -d /opt/compactdb/repository ]] && rm -rf /opt/compactdb/repository.previous
[[ -d /opt/compactdb/repository ]] && mv /opt/compactdb/repository /opt/compactdb/repository.previous
mv /opt/compactdb/repository.new /opt/compactdb/repository

# This export is intentionally for a PRIVATE repository and carries the live
# deployment configuration.  Install it before any deployment checks so a
# fresh VPS is non-interactive and reproducible.
private_config="$project_dir/private"
for required_config in bot.env deploy.env paths.env rclone.conf update.env; do
  [[ -s "$private_config/$required_config" ]] || {
    echo "Private deployment configuration is missing: $required_config" >&2
    exit 1
  }
done
install -o root -g root -m 0600 "$private_config/bot.env" /etc/compactdb/bot.env
install -o root -g root -m 0600 "$private_config/deploy.env" /etc/compactdb/deploy.env
install -o root -g root -m 0600 "$private_config/paths.env" /etc/compactdb/paths.env
install -o root -g root -m 0600 "$private_config/rclone.conf" /etc/compactdb/rclone.conf
install -o root -g root -m 0600 "$private_config/update.env" /etc/compactdb/update.env

install -m 0755 /opt/compactdb/repository/deploy/compactdb /usr/local/bin/compactdb
install -m 0755 /opt/compactdb/repository/deploy/compactdb-notify /usr/local/bin/compactdb-notify
install -m 0755 /opt/compactdb/repository/deploy/compactdb-observer /usr/local/bin/compactdb-observer
install -m 0755 /opt/compactdb/repository/deploy/compactdb-deploy /usr/local/sbin/compactdb-deploy
install -m 0755 /opt/compactdb/repository/deploy/compactdb-updater /usr/local/sbin/compactdb-updater
install -m 0644 /opt/compactdb/repository/deploy/compactdb-bot.service /etc/systemd/system/compactdb-bot.service
install -m 0644 /opt/compactdb/repository/deploy/compactdb-deploy.service /etc/systemd/system/compactdb-deploy.service
install -m 0644 /opt/compactdb/repository/deploy/compactdb-update.service /etc/systemd/system/compactdb-update.service
install -m 0644 /opt/compactdb/repository/deploy/compactdb-update.timer /etc/systemd/system/compactdb-update.timer
install -m 0644 /opt/compactdb/repository/deploy/compactdb-notify.service /etc/systemd/system/compactdb-notify.service
install -m 0644 /opt/compactdb/repository/deploy/compactdb-notify.timer /etc/systemd/system/compactdb-notify.timer

chmod 0600 /etc/compactdb/deploy.env
chmod 0600 /etc/compactdb/bot.env /etc/compactdb/paths.env /etc/compactdb/rclone.conf /etc/compactdb/update.env

cat >/etc/logrotate.d/compactdb <<'ROTATE'
/var/log/compactdb/*.log {
  size 10M
  rotate 4
  compress
  missingok
  notifempty
  copytruncate
  su root root
}
ROTATE

systemctl daemon-reload
systemctl enable compactdb-deploy.service compactdb-notify.timer >/dev/null
systemctl start compactdb-notify.timer
set +u
# shellcheck disable=SC1091
source /etc/compactdb/update.env
set -u
if [[ -z ${GITHUB_TOKEN:-} ]] || grep -q 'REPLACE_OWNER\|REPLACE_REPO' /etc/compactdb/update.env; then
  systemctl disable --now compactdb-update.timer >/dev/null 2>&1 || true
else
  systemctl enable --now compactdb-update.timer >/dev/null
fi
systemctl restart --no-block compactdb-deploy.service
echo 'CompactDB durable deployment started. Run: sudo compactdb status'
