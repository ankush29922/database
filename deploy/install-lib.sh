#!/usr/bin/env bash

compactdb_sync_repository() {
  local source=$1 target=$2 staging previous
  [[ -d "$source/app" && -d "$source/deploy" && -d "$source/private" && -f "$source/requirements.lock" ]] || return 1
  if [[ $(readlink -f "$source") == $(readlink -m "$target") ]]; then
    return 0
  fi
  staging="${target}.new.$$"
  previous="${target}.previous"
  rm -rf -- "$staging"
  install -d -m 0755 "$staging"
  cp -a "$source/app" "$source/deploy" "$source/private" "$source/requirements.lock" "$staging/"
  chown -R root:root "$staging"
  chmod -R go-w "$staging"
  chmod 0700 "$staging/private"
  find "$staging/private" -type f -exec chmod 0600 {} +
  rm -rf -- "$previous"
  [[ ! -d "$target" ]] || mv "$target" "$previous"
  mv "$staging" "$target"
}

compactdb_install_private_configuration() {
  local source=$1 target=$2 name
  install -d -o root -g root -m 0700 "$target"
  for name in bot.env deploy.env paths.env rclone.conf update.env; do
    [[ -s "$source/$name" ]] || return 1
    install -o root -g root -m 0600 "$source/$name" "$target/$name"
  done
}
