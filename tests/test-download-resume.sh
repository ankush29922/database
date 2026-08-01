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

cat >"$mock_bin/rclone" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$MOCK_STATE/arguments"
grep -Fq -- '--transfers 1' <<<"$*"
grep -Fq -- '--partial-suffix .partial' <<<"$*"
destination=$3
if [[ ! -f "$MOCK_STATE/interrupted" ]]; then
  touch "$MOCK_STATE/interrupted"
  printf 'partial-transfer' >"$destination/mock-file.partial"
  exit 9
fi
[[ -f "$destination/mock-file.partial" ]]
mv "$destination/mock-file.partial" "$destination/mock-file.complete"
MOCK
chmod 0755 "$mock_bin/rclone"

# shellcheck source=../deploy/download-lib.sh
source "$repository/deploy/download-lib.sh"
if PATH="$mock_bin:/usr/bin:/bin" MOCK_STATE="$mock_state" \
    compactdb_rclone_copy remote:folder "$destination" "$temporary/rclone.conf" 8; then
  printf 'mock transfer did not interrupt\n' >&2
  exit 1
fi
[[ -f "$destination/mock-file.partial" ]]
PATH="$mock_bin:/usr/bin:/bin" MOCK_STATE="$mock_state" \
  compactdb_rclone_copy remote:folder "$destination" "$temporary/rclone.conf" 8
[[ -f "$destination/mock-file.complete" && ! -e "$destination/mock-file.partial" ]]
[[ $(wc -l <"$mock_state/arguments") -eq 2 ]]
printf 'MOCK_INTERRUPTED_DOWNLOAD_RESUME=PASS\n'
