from __future__ import annotations

import atexit
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from lookup_index_common import (
    BUCKET_COUNT,
    EXPECTED_RECORDS,
)


LAYOUT_VERSION = "compactdb-direct-locator-v1"
ENTRY_BYTES = 4
BYTE_ORDER = "little"
EXPECTED_ENTRIES = EXPECTED_RECORDS
EXPECTED_BYTES = EXPECTED_ENTRIES * ENTRY_BYTES
DEFAULT_LOCATOR_PATH = Path(
    "/srv/compactdb/CompactDB-Portable/database/compactdb-direct-locator.bin"
)
DEFAULT_MANIFEST_PATH = Path(
    "/srv/compactdb/CompactDB-Portable/database/compactdb-direct-locator.json"
)


class DirectLocatorUnavailable(RuntimeError):
    pass


def bucket_entry_count(
    bucket: int,
    *,
    total: int = EXPECTED_ENTRIES,
    buckets: int = BUCKET_COUNT,
) -> int:
    if not 0 <= bucket < buckets:
        raise ValueError("bucket is out of range")
    quotient, remainder = divmod(total, buckets)
    return quotient + (1 if bucket < remainder else 0)


def bucket_base_entries(
    bucket: int,
    *,
    total: int = EXPECTED_ENTRIES,
    buckets: int = BUCKET_COUNT,
) -> int:
    if not 0 <= bucket <= buckets:
        raise ValueError("bucket is out of range")
    quotient, remainder = divmod(total, buckets)
    return bucket * quotient + min(bucket, remainder)


def locator_byte_offset(
    record_id: int,
    *,
    total: int = EXPECTED_ENTRIES,
    buckets: int = BUCKET_COUNT,
) -> int:
    record_id = int(record_id)
    if not 0 <= record_id < total:
        raise DirectLocatorUnavailable("record_id is outside locator range")
    bucket = record_id % buckets
    position = record_id // buckets
    if position >= bucket_entry_count(bucket, total=total, buckets=buckets):
        raise DirectLocatorUnavailable("record_id bucket position is invalid")
    return ENTRY_BYTES * (
        bucket_base_entries(bucket, total=total, buckets=buckets) + position
    )


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DirectLocatorUnavailable(
            f"cannot read direct-locator manifest: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise DirectLocatorUnavailable("direct-locator manifest is malformed")
    return value


class DirectLocator:
    def __init__(
        self,
        locator_path: Path,
        manifest_path: Path,
        *,
        expected_source: dict[str, Any],
        expected_entries: int = EXPECTED_ENTRIES,
        buckets: int = BUCKET_COUNT,
    ) -> None:
        self.path = locator_path.resolve(strict=True)
        self.manifest_path = manifest_path.resolve(strict=True)
        self.entries = int(expected_entries)
        self.buckets = int(buckets)
        self.expected_bytes = self.entries * ENTRY_BYTES
        manifest = read_manifest(self.manifest_path)
        self._validate_manifest(manifest, expected_source)
        info = self.path.stat()
        if not self.path.is_file() or int(info.st_size) != self.expected_bytes:
            raise DirectLocatorUnavailable("direct-locator file size is invalid")
        self.manifest = manifest
        self._fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        self._lock = threading.RLock()

    def _validate_manifest(
        self,
        manifest: dict[str, Any],
        expected_source: dict[str, Any],
    ) -> None:
        required = {
            "status": "READY",
            "layout_version": LAYOUT_VERSION,
            "byte_order": BYTE_ORDER,
            "entry_bytes": ENTRY_BYTES,
            "bucket_count": self.buckets,
            "total_entries": self.entries,
            "total_bytes": self.expected_bytes,
        }
        for key, expected in required.items():
            if manifest.get(key) != expected:
                raise DirectLocatorUnavailable(
                    f"direct-locator manifest field {key} is invalid"
                )
        if manifest.get("source_identity") != expected_source:
            raise DirectLocatorUnavailable("direct-locator source identity is stale")
        buckets = manifest.get("buckets")
        if not isinstance(buckets, list) or len(buckets) != self.buckets:
            raise DirectLocatorUnavailable("direct-locator bucket ledger is incomplete")
        for bucket, entry in enumerate(buckets):
            expected_count = bucket_entry_count(
                bucket, total=self.entries, buckets=self.buckets
            )
            expected_base = bucket_base_entries(
                bucket, total=self.entries, buckets=self.buckets
            )
            if (
                not isinstance(entry, dict)
                or entry.get("bucket") != bucket
                or entry.get("base_entry") != expected_base
                or entry.get("count") != expected_count
                or entry.get("bytes") != expected_count * ENTRY_BYTES
                or entry.get("readback_verified") is not True
                or not isinstance(entry.get("sha256"), str)
                or len(entry["sha256"]) != 64
            ):
                raise DirectLocatorUnavailable(
                    f"direct-locator bucket {bucket} manifest is invalid"
                )

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

    def lookup(self, record_id: int) -> int:
        offset = locator_byte_offset(
            record_id, total=self.entries, buckets=self.buckets
        )
        with self._lock:
            if self._fd < 0:
                raise DirectLocatorUnavailable("direct locator is closed")
            payload = os.pread(self._fd, ENTRY_BYTES, offset)
        if len(payload) != ENTRY_BYTES:
            raise DirectLocatorUnavailable(
                "direct-locator positional read was incomplete"
            )
        return int.from_bytes(payload, BYTE_ORDER, signed=False)

    def lookup_many(self, record_ids: Iterable[int]) -> list[tuple[int, int]]:
        values = sorted(dict.fromkeys(int(value) for value in record_ids))
        return [(record_id, self.lookup(record_id)) for record_id in values]


_RUNTIME_LOCK = threading.RLock()
_RUNTIME_LOCATOR: DirectLocator | None = None
_RUNTIME_KEY: tuple[str, str, int, int] | None = None


def open_runtime_locator(
    *,
    locator_path: Path = DEFAULT_LOCATOR_PATH,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    sidecar_path: Path,
    final_path: Path,
    expected_entries: int = EXPECTED_ENTRIES,
    buckets: int = BUCKET_COUNT,
    source_identity_provider: Callable[[], dict[str, Any]] | None = None,
) -> DirectLocator:
    global _RUNTIME_LOCATOR, _RUNTIME_KEY
    key = (
        str(locator_path.resolve()),
        str(manifest_path.resolve()),
        int(expected_entries),
        int(buckets),
    )
    with _RUNTIME_LOCK:
        if _RUNTIME_LOCATOR is not None and _RUNTIME_KEY == key:
            return _RUNTIME_LOCATOR
        close_runtime_locator()
        provider = source_identity_provider
        if provider is None:
            provider = lambda: read_manifest(manifest_path)["source_identity"]
        locator = DirectLocator(
            locator_path,
            manifest_path,
            expected_source=provider(),
            expected_entries=expected_entries,
            buckets=buckets,
        )
        _RUNTIME_LOCATOR = locator
        _RUNTIME_KEY = key
        return locator


def close_runtime_locator() -> None:
    global _RUNTIME_LOCATOR, _RUNTIME_KEY
    with _RUNTIME_LOCK:
        if _RUNTIME_LOCATOR is not None:
            _RUNTIME_LOCATOR.close()
        _RUNTIME_LOCATOR = None
        _RUNTIME_KEY = None


atexit.register(close_runtime_locator)
