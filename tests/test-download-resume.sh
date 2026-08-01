#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-download.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
mock_bin="$temporary/bin"
mock_state="$temporary/state"
destination="$temporary/CompactDB-Portable"
install -d "$mock_bin" "$mock_state" "$destination"
touch "$temporary/rclone.conf"
printf 'completed-data' >"$destination/completed-file"
printf 'partial-data' >"$destination/incomplete-file.partial"

cat >"$mock_bin/rclone" <<'MOCK'
#!/usr/bin/env bash
printf '%s\t%s\n' "$MOCK_RCLONE_VERSION" "$*" >>"$MOCK_STATE/arguments"
grep -Fq -- '--transfers 1' <<<"$*"
grep -Fq -- '--ignore-existing' <<<"$*"
if grep -Fq -- '--partial-suffix' <<<"$*"; then
  printf 'unsupported partial suffix passed to rclone %s\n' "$MOCK_RCLONE_VERSION" >&2
  exit 64
fi
destination=$3
[[ -f "$destination/completed-file" && -f "$destination/incomplete-file.partial" ]]
if [[ ! -f "$MOCK_STATE/interrupted" ]]; then
  touch "$MOCK_STATE/interrupted"
  exit 9
fi
printf 'new-data' >"$destination/new-file"
MOCK
chmod 0755 "$mock_bin/rclone"

# shellcheck source=../deploy/download-lib.sh
source "$repository/deploy/download-lib.sh"
if PATH="$mock_bin:/usr/bin:/bin" MOCK_STATE="$mock_state" MOCK_RCLONE_VERSION=1.60.1 \
    compactdb_rclone_copy remote:folder "$destination" "$temporary/rclone.conf" 8; then
  printf 'mock transfer did not interrupt\n' >&2
  exit 1
fi
[[ -f "$destination/completed-file" && -f "$destination/incomplete-file.partial" ]]
PATH="$mock_bin:/usr/bin:/bin" MOCK_STATE="$mock_state" MOCK_RCLONE_VERSION=1.60.1 \
  compactdb_rclone_copy remote:folder "$destination" "$temporary/rclone.conf" 8
PATH="$mock_bin:/usr/bin:/bin" MOCK_STATE="$mock_state" MOCK_RCLONE_VERSION=1.70.0 \
  compactdb_rclone_copy remote:folder "$destination" "$temporary/rclone.conf" 4
[[ -f "$destination/completed-file" && -f "$destination/incomplete-file.partial" && -f "$destination/new-file" ]]
[[ $(wc -l <"$mock_state/arguments") -eq 3 ]]
! rg -F -- '--partial-suffix' "$mock_state/arguments"
[[ $(rg -c -F -- '--ignore-existing' "$mock_state/arguments") -eq 3 ]]
printf 'MOCK_INTERRUPTED_DOWNLOAD_RESUME=PASS\n'
printf 'MOCK_RCLONE_1_60_COMPATIBILITY=PASS\n'
printf 'MOCK_NEWER_RCLONE_COMPATIBILITY=PASS\n'
printf 'MOCK_COMPLETED_AND_PARTIAL_FILES_PRESERVED=PASS\n'
