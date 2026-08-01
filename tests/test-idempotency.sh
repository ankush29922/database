#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-idempotency.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
# shellcheck source=../deploy/install-lib.sh
source "$repository/deploy/install-lib.sh"

target="$temporary/opt/compactdb/repository"
configuration="$temporary/etc/compactdb"
install -d "$(dirname "$target")"

compactdb_sync_repository "$repository" "$target"
compactdb_install_private_configuration "$repository/private" "$configuration"
find "$target" "$configuration" -printf '%P %y %m %s\n' | sort >"$temporary/first.metadata"

compactdb_sync_repository "$repository" "$target"
compactdb_install_private_configuration "$repository/private" "$configuration"
find "$target" "$configuration" -printf '%P %y %m %s\n' | sort >"$temporary/second.metadata"

cmp -s "$temporary/first.metadata" "$temporary/second.metadata"
[[ $(find "$configuration" -maxdepth 1 -type f | wc -l) -eq 5 ]]
[[ $(stat -c '%a' "$configuration") == 700 ]]
while IFS= read -r file; do
  [[ $(stat -c '%a' "$file") == 600 ]]
done < <(find "$configuration" -maxdepth 1 -type f)
printf 'MOCK_IDEMPOTENCY=PASS\n'
