#!/usr/bin/env bash

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
