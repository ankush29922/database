#!/usr/bin/env bash
set -Eeuo pipefail

package_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$package_root/deploy/install.sh"
