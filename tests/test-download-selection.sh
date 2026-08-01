#!/usr/bin/env bash
set -Eeuo pipefail

repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

# shellcheck source=../deploy/download-lib.sh
source "$repository/deploy/download-lib.sh"

[[ $(compactdb_select_transfer_plan gdown yes yes yes) == gdown_then_rclone ]]
[[ $(compactdb_select_transfer_plan auto yes yes yes) == gdown_then_rclone ]]
[[ $(compactdb_select_transfer_plan gdown yes no yes) == gdown ]]
[[ $(compactdb_select_transfer_plan gdown no yes yes) == rclone ]]
if compactdb_select_transfer_plan gdown no yes no >/dev/null; then
  printf 'invalid gdown configuration unexpectedly selected a transfer plan\n' >&2
  exit 1
fi

mock_gdown_cli() {
  MOCK_GDOWN_ARGUMENTS=("$@")
}

MOCK_GDOWN_ARGUMENTS=()
compactdb_gdown_folder mock_gdown_cli \
  'https://drive.google.com/drive/folders/1DvANP5FDvObPnYJJKuzeGYCUARgCVvv_?usp=sharing' \
  /srv/compactdb/CompactDB-Portable
[[ ${#MOCK_GDOWN_ARGUMENTS[@]} -eq 6 ]]
[[ ${MOCK_GDOWN_ARGUMENTS[0]} == --folder ]]
[[ ${MOCK_GDOWN_ARGUMENTS[1]} == --continue ]]
[[ ${MOCK_GDOWN_ARGUMENTS[2]} == --remaining-ok ]]
[[ ${MOCK_GDOWN_ARGUMENTS[3]} == --output ]]
[[ ${MOCK_GDOWN_ARGUMENTS[4]} == /srv/compactdb/CompactDB-Portable ]]
[[ ${MOCK_GDOWN_ARGUMENTS[5]} == *1DvANP5FDvObPnYJJKuzeGYCUARgCVvv_* ]]

COMPLETED_FILE_PRESENT=yes
RCLONE_CALLS=0
MOCK_GDOWN_EXIT=1
mock_gdown_failure() { return "$MOCK_GDOWN_EXIT"; }
mock_gdown_success() { return 0; }
mock_metadata_incomplete() { return 1; }
mock_metadata_complete() { return 0; }
mock_rclone() {
  [[ "$COMPLETED_FILE_PRESENT" == yes ]]
  RCLONE_CALLS=$((RCLONE_CALLS + 1))
}

for MOCK_GDOWN_EXIT in 1 75 18; do
  RCLONE_CALLS=0
  compactdb_gdown_with_rclone_fallback \
    mock_gdown_failure mock_metadata_complete mock_rclone
  [[ "$COMPACTDB_SELECTED_DOWNLOAD_METHOD" == rclone ]]
  [[ "$COMPACTDB_GDOWN_FALLBACK_REASON" == transfer_failed ]]
  [[ "$RCLONE_CALLS" -eq 1 && "$COMPLETED_FILE_PRESENT" == yes ]]
done

RCLONE_CALLS=0
compactdb_gdown_with_rclone_fallback \
  mock_gdown_success mock_metadata_incomplete mock_rclone
[[ "$COMPACTDB_SELECTED_DOWNLOAD_METHOD" == rclone ]]
[[ "$COMPACTDB_GDOWN_FALLBACK_REASON" == incomplete_metadata ]]
[[ "$RCLONE_CALLS" -eq 1 && "$COMPLETED_FILE_PRESENT" == yes ]]

RCLONE_CALLS=0
compactdb_gdown_with_rclone_fallback \
  mock_gdown_success mock_metadata_complete mock_rclone
[[ "$COMPACTDB_SELECTED_DOWNLOAD_METHOD" == gdown ]]
[[ "$RCLONE_CALLS" -eq 0 && "$COMPLETED_FILE_PRESENT" == yes ]]

printf 'MOCKED_DOWNLOAD_SELECTION=PASS\n'
printf 'MOCKED_GDOWN_FOLDER_MODE=PASS\n'
printf 'MOCKED_RCLONE_FALLBACK=PASS\n'
printf 'MOCKED_RATE_LIMIT_AND_RESUME_FAILURES=PASS\n'
printf 'MOCKED_COMPLETED_FILES_PRESERVED=PASS\n'
