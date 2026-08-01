#!/usr/bin/env bash

compactdb_select_transfer_plan() {
  local method=$1 gdown_configured=$2 rclone_configured=$3 fallback=$4
  case "$method" in
    rclone)
      [[ "$rclone_configured" == yes ]] || return 1
      printf 'rclone\n'
      ;;
    gdown)
      if [[ "$gdown_configured" == yes && "$fallback" == yes && "$rclone_configured" == yes ]]; then
        printf 'gdown_then_rclone\n'
      elif [[ "$gdown_configured" == yes ]]; then
        printf 'gdown\n'
      elif [[ "$fallback" == yes && "$rclone_configured" == yes ]]; then
        printf 'rclone\n'
      else
        return 1
      fi
      ;;
    auto)
      if [[ "$gdown_configured" == yes && "$rclone_configured" == yes ]]; then
        printf 'gdown_then_rclone\n'
      elif [[ "$gdown_configured" == yes ]]; then
        printf 'gdown\n'
      elif [[ "$rclone_configured" == yes ]]; then
        printf 'rclone\n'
      else
        return 1
      fi
      ;;
    *)
      return 1
      ;;
  esac
}

compactdb_rclone_copy() {
  local source=$1 destination=$2 configuration=$3 streams=$4
  rclone copy "$source" "$destination" \
    --config "$configuration" \
    --transfers 1 --checkers 2 --buffer-size 8M \
    --multi-thread-streams "$streams" --multi-thread-cutoff 256M \
    --partial-suffix .partial \
    --ignore-checksum --use-mmap --bwlimit off \
    --retries 20 --retries-sleep 30s --low-level-retries 20 \
    --timeout 5m --contimeout 30s --stats 15s --stats-one-line --log-level ERROR
}

compactdb_gdown_folder() {
  local executable=$1 source_url=$2 destination=$3
  "$executable" --folder --continue --remaining-ok --output "$destination" "$source_url"
}

compactdb_gdown_with_rclone_fallback() {
  local gdown_runner=$1 metadata_validator=$2 rclone_runner=$3
  COMPACTDB_SELECTED_DOWNLOAD_METHOD=
  COMPACTDB_GDOWN_FALLBACK_REASON=
  if "$gdown_runner"; then
    if "$metadata_validator"; then
      COMPACTDB_SELECTED_DOWNLOAD_METHOD=gdown
      return 0
    fi
    COMPACTDB_GDOWN_FALLBACK_REASON=incomplete_metadata
  else
    COMPACTDB_GDOWN_FALLBACK_REASON=transfer_failed
  fi
  "$rclone_runner" || return "$?"
  COMPACTDB_SELECTED_DOWNLOAD_METHOD=rclone
}
