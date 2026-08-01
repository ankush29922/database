from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
LEGACY_ENV_PATH = BASE_DIR / ".env"
if LEGACY_ENV_PATH.exists():
    load_dotenv(LEGACY_ENV_PATH, override=False)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_set(name: str) -> frozenset[int]:
    values: set[int] = set()
    for item in os.getenv(name, "").split(","):
        item = item.strip()
        if not item:
            continue
        if not item.isascii() or not item.isdecimal():
            raise RuntimeError(f"{name} must contain comma-separated Telegram user IDs")
        values.add(int(item))
    return frozenset(values)


# Telegram and access control.
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
OWNER_IDS = _int_set("BOT_OWNER_IDS")
ALLOWED_USER_IDS = _int_set("ALLOWED_USER_IDS")
AUTHORIZED_USER_IDS = OWNER_IDS | ALLOWED_USER_IDS

# Immutable CompactDB.
DUCKDB_PATH = Path(
    os.getenv(
        "DUCKDB_PATH",
        "/srv/compactdb/CompactDB-Portable/database/compactdb-final.duckdb",
    )
).expanduser()
LOOKUP_SIDECAR_PATH = Path(
    os.getenv(
        "LOOKUP_SIDECAR_PATH",
        "/srv/compactdb/CompactDB-Portable/database/compactdb-lookup.duckdb",
    )
).expanduser()
DIRECT_LOCATOR_PATH = Path(
    os.getenv(
        "DIRECT_LOCATOR_PATH",
        "/srv/compactdb/CompactDB-Portable/database/compactdb-direct-locator.bin",
    )
).expanduser()
DIRECT_LOCATOR_MANIFEST_PATH = Path(
    os.getenv(
        "DIRECT_LOCATOR_MANIFEST_PATH",
        "/srv/compactdb/CompactDB-Portable/database/compactdb-direct-locator.json",
    )
).expanduser()
DUCKDB_THREADS = 1
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "384MB").strip()
DUCKDB_MAX_TEMP_BYTES = min(
    128 * 1024**2,
    max(16 * 1024**2, int(os.getenv("DUCKDB_MAX_TEMP_BYTES", str(128 * 1024**2)))),
)
DUCKDB_MAX_CONCURRENT_QUERIES = min(
    2, max(1, int(os.getenv("DUCKDB_MAX_CONCURRENT_QUERIES", "1")))
)

# Behavior and formatting.
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "IN").strip().upper()
DELETE_AFTER_SECONDS = max(1, int(os.getenv("DELETE_AFTER_SECONDS", "60")))
CSV_MAX_ROWS = max(1, int(os.getenv("CSV_MAX_ROWS", "5000")))
CSV_HAS_HEADER = _bool(os.getenv("CSV_HAS_HEADER", "0"))
CSV_QUERY_CHUNK_ROWS = max(1, min(500, int(os.getenv("CSV_QUERY_CHUNK_ROWS", "100"))))

ENABLE_QUERY_CACHE = _bool(os.getenv("ENABLE_QUERY_CACHE", "0"), False)
CACHE_TTL_SECONDS = max(1, int(os.getenv("CACHE_TTL_SECONDS", "300")))
MAX_CACHE_SIZE = max(1, int(os.getenv("MAX_CACHE_SIZE", "1000")))

RESULT_MESSAGE_MAX_CHARS = min(
    3900, max(1000, int(os.getenv("RESULT_MESSAGE_MAX_CHARS", "3500")))
)
RESULT_MAX_MESSAGES = max(1, min(20, int(os.getenv("RESULT_MAX_MESSAGES", "8"))))
RESULT_FILE_FALLBACK = _bool(os.getenv("RESULT_FILE_FALLBACK", "1"), True)
MAX_IN_MEMORY_RESULTS = max(
    10, min(5000, int(os.getenv("MAX_IN_MEMORY_RESULTS", "500")))
)

RUNTIME_DIR = Path(
    os.getenv("COMPACTDB_RUNTIME_DIR", "/var/lib/compactdb/runtime")
).expanduser()
SETTINGS_FILE = Path(
    os.getenv("SETTINGS_FILE", str(RUNTIME_DIR / "settings.json"))
).expanduser()
DUCKDB_TEMP_DIR = Path(
    os.getenv("DUCKDB_TEMP_DIR", "/var/lib/compactdb/duckdb-temp")
).expanduser()

QUERY_TIMEOUT_SECONDS = max(
    2.0, min(60.0, float(os.getenv("QUERY_TIMEOUT_SECONDS", "15")))
)

# Installed merger evidence checked by health_check.py.
MERGE_STATE_PATH = Path("/home/xen/.local/state/duckdb-merge/merge-state.json")
VERIFY_REPORT_PATH = Path(
    "/home/xen/.local/share/duckdb-merge/reports/final-verification-report.json"
)
EXPECTED_FINAL_DATABASE_BYTES = 188_926_144_512


def is_owner(user_id: int) -> bool:
    return int(user_id) in OWNER_IDS


def is_allowed(user_id: int) -> bool:
    return int(user_id) in AUTHORIZED_USER_IDS


def validate_config(*, require_bot_token: bool = True) -> None:
    if require_bot_token and not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required")
    if not OWNER_IDS:
        raise RuntimeError("BOT_OWNER_IDS must contain at least one owner")
    if not DEFAULT_REGION:
        raise RuntimeError("DEFAULT_REGION cannot be empty")
    if DUCKDB_PATH.suffix.lower() != ".duckdb":
        raise RuntimeError("DUCKDB_PATH must identify a .duckdb file")
    if LOOKUP_SIDECAR_PATH.suffix.lower() != ".duckdb":
        raise RuntimeError("LOOKUP_SIDECAR_PATH must identify a .duckdb file")
    if DIRECT_LOCATOR_PATH.suffix.lower() != ".bin":
        raise RuntimeError("DIRECT_LOCATOR_PATH must identify a .bin file")
    if DIRECT_LOCATOR_MANIFEST_PATH.suffix.lower() != ".json":
        raise RuntimeError(
            "DIRECT_LOCATOR_MANIFEST_PATH must identify a .json file"
        )
