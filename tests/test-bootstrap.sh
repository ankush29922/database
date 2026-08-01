#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d "$repository/.test-bootstrap.XXXXXX")
trap 'find "$temporary" -depth -delete 2>/dev/null || true' EXIT
mock_bin="$temporary/bin"
install -d "$mock_bin"

cat >"$mock_bin/apt-get" <<'MOCK'
#!/usr/bin/env bash
printf 'apt-get %s\n' "$*" >>"$COMPACTDB_TEST_LOG"
MOCK
cat >"$mock_bin/git" <<'MOCK'
#!/usr/bin/env bash
[[ ${1:-} == clone ]] || exit 2
destination=${!#}
mkdir -p "$destination"
cp -a "$COMPACTDB_TEST_REPOSITORY/." "$destination/"
printf 'git-clone\n' >>"$COMPACTDB_TEST_LOG"
MOCK
chmod 0755 "$mock_bin/apt-get" "$mock_bin/git"

output=$(
  PATH="$mock_bin:/usr/bin:/bin" \
  COMPACTDB_TEST_LOG="$temporary/calls.log" \
  COMPACTDB_TEST_REPOSITORY="$repository" \
  COMPACTDB_INSTALL_TEST_STOP_AFTER_MODE=1 \
    bash <"$repository/deploy/install.sh"
)
grep -Fqx 'COMPACTDB_INSTALL_MODE=bootstrap' <<<"$output"
grep -Fqx 'COMPACTDB_INSTALL_MODE=repository' <<<"$output"
grep -Fqx 'git-clone' "$temporary/calls.log"

if PATH="$mock_bin:/usr/bin:/bin" \
   COMPACTDB_TEST_LOG="$temporary/calls.log" \
   COMPACTDB_TEST_REPOSITORY="$repository" \
   COMPACTDB_BOOTSTRAP_ACTIVE=1 \
   COMPACTDB_INSTALL_TEST_STOP_AFTER_MODE=1 \
     bash <"$repository/deploy/install.sh" >/dev/null 2>&1; then
  printf 'recursion guard did not reject nested bootstrap\n' >&2
  exit 1
fi
printf 'MOCK_BOOTSTRAP=PASS\n'
