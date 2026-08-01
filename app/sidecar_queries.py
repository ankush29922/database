from __future__ import annotations

import atexit
import bisect
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import duckdb

from config import (
    DIRECT_LOCATOR_MANIFEST_PATH,
    DIRECT_LOCATOR_PATH,
    DUCKDB_MAX_TEMP_BYTES,
    DUCKDB_MEMORY_LIMIT,
    DUCKDB_PATH,
    DUCKDB_TEMP_DIR,
    DUCKDB_THREADS,
    LOOKUP_SIDECAR_PATH,
    MAX_IN_MEMORY_RESULTS,
)
from direct_locator import open_runtime_locator
from direct_locator import read_manifest
from lookup_index_common import (
    BUCKET_COUNT,
    EXPECTED_RECORDS,
    SidecarUnavailable,
    id_bucket,
    id_key,
    locator_bucket,
    phone_bucket,
    phone_key,
)


OUTPUT_COLUMNS = (
    "match_type",
    "record_id",
    "record_type",
    "field_order",
    "presence_mask",
    "type_mask",
    "mobile",
    "lookup_phone",
    "phone_partition",
    "name",
    "fname",
    "address",
    "alt",
    "circle",
    "id",
    "email",
    "exception_reason",
)

PAYLOAD_FETCH_MODE = "DIRECT_LOCATOR_PHYSICAL_ROWID"
PAYLOAD_FETCH_BATCH_SIZE = 128
_CONNECTION_LOCK = threading.RLock()
_CONNECTIONS: dict[
    str, tuple[tuple[str, int, int, int, int], duckdb.DuckDBPyConnection]
] = {}
_ROW_GROUP_CACHE: tuple[
    tuple[str, int, int, int, int],
    tuple[tuple[int, int, int, int], ...],
] | None = None
_READY_CACHE: tuple[
    tuple[tuple[str, int, int, int, int], tuple[str, int, int, int, int]],
    dict[str, Any],
] | None = None
_STORAGE_MIN_MAX = re.compile(r"\[Min:\s*(\d+),\s*Max:\s*(\d+)\]")
DIRECT_LOCATOR_EXPECTED_ENTRIES = EXPECTED_RECORDS
DIRECT_LOCATOR_BUCKETS = BUCKET_COUNT
_DIRECT_LOCATOR_SOURCE_IDENTITY_PROVIDER = None


def _sql_string(value: str) -> str:
    return value.replace("'", "''")


def _file_identity(path: Path) -> tuple[str, int, int, int, int]:
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise SidecarUnavailable(f"database path is unavailable: {path}") from exc
    if not resolved.is_file():
        raise SidecarUnavailable(f"database path is not a file: {resolved}")
    return (
        str(resolved),
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _configure_connection(
    connection: duckdb.DuckDBPyConnection,
) -> duckdb.DuckDBPyConnection:
    connection.execute(f"SET threads={DUCKDB_THREADS}")
    connection.execute(f"SET memory_limit='{_sql_string(DUCKDB_MEMORY_LIMIT)}'")
    DUCKDB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    connection.execute(
        f"SET temp_directory='{_sql_string(str(DUCKDB_TEMP_DIR))}'"
    )
    connection.execute(
        f"SET max_temp_directory_size='{DUCKDB_MAX_TEMP_BYTES}B'"
    )
    connection.execute("SET enable_progress_bar=false")
    connection.execute("SET preserve_insertion_order=false")
    return connection


@contextmanager
def _connection(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    identity = _file_identity(path)
    sidecar_identity = _file_identity(LOOKUP_SIDECAR_PATH)
    role = "sidecar" if identity == sidecar_identity else "main"
    with _CONNECTION_LOCK:
        existing = _CONNECTIONS.get(role)
        if existing is None or existing[0] != identity:
            if existing is not None:
                existing[1].close()
            connection = _configure_connection(
                duckdb.connect(identity[0], read_only=True)
            )
            _CONNECTIONS[role] = (identity, connection)
        else:
            connection = existing[1]
        yield connection


def close_persistent_connections() -> None:
    global _ROW_GROUP_CACHE, _READY_CACHE
    with _CONNECTION_LOCK:
        for _identity, connection in _CONNECTIONS.values():
            try:
                connection.close()
            except Exception:
                pass
        _CONNECTIONS.clear()
        _ROW_GROUP_CACHE = None
        _READY_CACHE = None


def interrupt_persistent_connections() -> None:
    """Interrupt the one active bounded lookup without exposing its inputs."""
    for _identity, connection in tuple(_CONNECTIONS.values()):
        try:
            connection.interrupt()
        except Exception:
            pass


atexit.register(close_persistent_connections)


def ensure_ready() -> dict[str, Any]:
    global _READY_CACHE
    identities = (
        _file_identity(DUCKDB_PATH),
        _file_identity(LOOKUP_SIDECAR_PATH),
    )
    if _READY_CACHE is not None and _READY_CACHE[0] == identities:
        return _READY_CACHE[1]
    manifest = read_manifest(DIRECT_LOCATOR_MANIFEST_PATH)
    recorded = manifest.get("source_identity", {})
    expected_final = int(recorded.get("final_source", {}).get("size", -1))
    expected_sidecar = int(recorded.get("sidecar_file", {}).get("size", -1))
    if identities[0][3] != expected_final or identities[1][3] != expected_sidecar:
        raise SidecarUnavailable("portable database file sizes do not match the locator manifest")
    ready = {"status": "READY", "identity": identities, "metadata": {"status": "READY"}}
    _READY_CACHE = (identities, ready)
    return ready


def initialize_direct_locator() -> dict[str, Any]:
    ensure_ready()
    locator = open_runtime_locator(
        locator_path=DIRECT_LOCATOR_PATH,
        manifest_path=DIRECT_LOCATOR_MANIFEST_PATH,
        sidecar_path=LOOKUP_SIDECAR_PATH,
        final_path=DUCKDB_PATH,
        expected_entries=DIRECT_LOCATOR_EXPECTED_ENTRIES,
        buckets=DIRECT_LOCATOR_BUCKETS,
        source_identity_provider=(
            _DIRECT_LOCATOR_SOURCE_IDENTITY_PROVIDER
            or (lambda: read_manifest(DIRECT_LOCATOR_MANIFEST_PATH)["source_identity"])
        ),
    )
    return {
        "status": locator.manifest["status"],
        "bytes": locator.expected_bytes,
        "mode": "DIRECT_LOCATOR_PHYSICAL_ROWID",
    }


def _values(rows: Sequence[Sequence[Any]], columns: int) -> tuple[str, list[Any]]:
    if not rows:
        raise ValueError("at least one lookup value is required")
    placeholders = ",".join(
        "(" + ",".join("?" for _ in range(columns)) + ")" for _ in rows
    )
    return placeholders, [value for row in rows for value in row]


def _phone_record_ids(candidates: Sequence[str], limit: int) -> list[int]:
    wanted = tuple(
        dict.fromkeys(
            (phone_bucket(phone_key(value)), phone_key(value))
            for value in candidates
        )
    )
    branches = []
    parameters: list[Any] = []
    for bucket, encoded in wanted:
        branches.append(
            "SELECT record_id FROM phone_lookup "
            "WHERE bucket=? AND phone_key=?"
        )
        parameters.extend((bucket, encoded))
    with _connection(LOOKUP_SIDECAR_PATH) as connection:
        rows = connection.execute(
            f"""
            SELECT record_id FROM (
              {' UNION ALL '.join(branches)}
            ) exact_phone_buckets
            ORDER BY record_id
            LIMIT ?
            """,
            [*parameters, int(limit)],
        ).fetchall()
    return sorted(dict.fromkeys(int(row[0]) for row in rows))[:limit]


def _id_record_ids(id_values: Sequence[str], limit: int) -> list[int]:
    wanted = tuple(
        dict.fromkeys(
            (id_bucket(id_key(value)), id_key(value), value)
            for value in id_values
        )
    )
    branches = []
    parameters: list[Any] = []
    for bucket, encoded, _id_text in wanted:
        branches.append(
            "SELECT record_id FROM id_lookup "
            "WHERE bucket=? AND id_key=?"
        )
        parameters.extend((bucket, encoded))
    with _connection(LOOKUP_SIDECAR_PATH) as connection:
        rows = connection.execute(
            f"""
            SELECT record_id FROM (
              {' UNION ALL '.join(branches)}
            ) exact_id_buckets
            ORDER BY record_id
            LIMIT ?
            """,
            [*parameters, int(limit)],
        ).fetchall()
    return sorted(dict.fromkeys(int(row[0]) for row in rows))[:limit]


def _locators(record_ids: Sequence[int]) -> list[tuple[int, int]]:
    wanted = sorted(dict.fromkeys(int(value) for value in record_ids))
    if not wanted:
        return []
    try:
        locator = open_runtime_locator(
            locator_path=DIRECT_LOCATOR_PATH,
            manifest_path=DIRECT_LOCATOR_MANIFEST_PATH,
            sidecar_path=LOOKUP_SIDECAR_PATH,
            final_path=DUCKDB_PATH,
            expected_entries=DIRECT_LOCATOR_EXPECTED_ENTRIES,
            buckets=DIRECT_LOCATOR_BUCKETS,
            source_identity_provider=(
                _DIRECT_LOCATOR_SOURCE_IDENTITY_PROVIDER
                or (lambda: read_manifest(DIRECT_LOCATOR_MANIFEST_PATH)["source_identity"])
            ),
        )
        return locator.lookup_many(wanted)
    except Exception as exc:
        if isinstance(exc, SidecarUnavailable):
            raise
        raise SidecarUnavailable(
            f"direct record locator is unavailable: {exc}"
        ) from exc


def _normal_projection(alias: str) -> str:
    return f"""
      {alias}.record_id,'NORMAL'::VARCHAR AS record_type,
      {alias}.field_order,{alias}.presence_mask,{alias}.type_mask,
      {alias}.mobile,{alias}.lookup_phone,{alias}.phone_partition,
      {alias}.name,{alias}.fname,{alias}.address,{alias}.alt,
      {alias}.circle,{alias}.id_value AS id,{alias}.email,
      NULL::VARCHAR AS exception_reason
    """


def _record_row_groups(
    connection: duckdb.DuckDBPyConnection,
) -> tuple[tuple[int, int, int, int], ...]:
    global _ROW_GROUP_CACHE
    identity = _file_identity(DUCKDB_PATH)
    if _ROW_GROUP_CACHE is not None and _ROW_GROUP_CACHE[0] == identity:
        return _ROW_GROUP_CACHE[1]
    rows = connection.execute(
        """
        SELECT row_group_id,start,count,stats
        FROM pragma_storage_info('records')
        WHERE column_name='record_id' AND segment_type<>'VALIDITY'
        ORDER BY row_group_id,start
        """
    ).fetchall()
    grouped: dict[int, list[int]] = {}
    for row_group_id, start, count, stats in rows:
        match = _STORAGE_MIN_MAX.search(str(stats))
        if match is None:
            raise SidecarUnavailable(
                f"record_id statistics missing for row group {row_group_id}"
            )
        values = grouped.setdefault(
            int(row_group_id),
            [int(start), int(start) + int(count), int(match[1]), int(match[2])],
        )
        values[0] = min(values[0], int(start))
        values[1] = max(values[1], int(start) + int(count))
        values[2] = min(values[2], int(match[1]))
        values[3] = max(values[3], int(match[2]))
    groups = tuple(tuple(values) for _key, values in sorted(grouped.items()))
    if not groups or any(
        row_start < 0
        or row_end <= row_start
        or record_min < 0
        or record_max < record_min
        for row_start, row_end, record_min, record_max in groups
    ):
        raise SidecarUnavailable("records row-group metadata is unavailable")
    for previous, current in zip(groups, groups[1:]):
        if previous[1] > current[0]:
            raise SidecarUnavailable("physical row groups overlap")
    _ROW_GROUP_CACHE = (identity, groups)
    return groups


def _group_locators(
    locators: Sequence[tuple[int, int]],
    row_groups: Sequence[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int, list[int]]]:
    unique: dict[int, int] = {}
    for record_id, row_location in locators:
        previous = unique.setdefault(int(record_id), int(row_location))
        if previous != int(row_location):
            raise SidecarUnavailable("record locator has conflicting locations")
    starts = [start for start, _end, _minimum, _maximum in row_groups]
    grouped: dict[tuple[int, int, int, int], list[int]] = {}
    for record_id, row_location in unique.items():
        position = bisect.bisect_right(starts, row_location) - 1
        if position < 0:
            raise SidecarUnavailable("record locator is outside physical row groups")
        start, end, record_min, record_max = row_groups[position]
        if not start <= row_location < end:
            raise SidecarUnavailable("record locator is outside physical row groups")
        if not record_min <= record_id <= record_max:
            raise SidecarUnavailable(
                "record locator disagrees with physical record_id statistics"
            )
        grouped.setdefault(
            (start, end, record_min, record_max), []
        ).append(record_id)
    return [
        (start, end, record_min, record_max, sorted(ids))
        for (start, end, record_min, record_max), ids in sorted(grouped.items())
    ]


def payload_query_sql(record_count: int, filter_count: int = 0) -> str:
    if record_count < 1 or record_count > PAYLOAD_FETCH_BATCH_SIZE:
        raise ValueError("payload record batch is out of bounds")
    id_placeholders = ",".join("?::UINTEGER" for _ in range(record_count))
    exact_filter = ""
    if filter_count:
        filter_placeholders = ",".join("?" for _ in range(filter_count))
        exact_filter = f" AND {{filter_column}} IN ({filter_placeholders})"
    return f"""
        SELECT ?::VARCHAR AS match_type,{_normal_projection("r")}
        FROM records r
        WHERE r.rowid>=? AND r.rowid<?
          AND r.record_id>=?::UINTEGER AND r.record_id<=?::UINTEGER
          AND r.record_id IN ({id_placeholders})
          {exact_filter}
        ORDER BY r.record_id
    """


def validate_payload_fetch_mode() -> dict[str, Any]:
    sql = physical_rowid_query_sql()
    upper = sql.upper()
    forbidden = (
        " OFFSET ",
        "ROW_NUMBER",
        "CAST(R.RECORD_ID",
        "MIN(RECORD_ID)",
        " JOIN ",
        "R.LOOKUP_PHONE IN",
        "R.RECORD_ID IN",
        "R.RECORD_ID>=",
        "R.RECORD_ID<=",
    )
    if (
        "R.ROWID=?" not in upper
        or "R.RECORD_ID=?::UINTEGER" not in upper
        or any(value in upper for value in forbidden)
    ):
        raise RuntimeError("physical-rowid payload SQL guard failed")
    return {
        "status": "PASS",
        "mode": PAYLOAD_FETCH_MODE,
        "batch_size": PAYLOAD_FETCH_BATCH_SIZE,
        "persistent_connections": True,
        "row_group_predicate": False,
        "physical_rowid": True,
        "record_locator_status": "VERIFIED",
    }


def physical_rowid_query_sql() -> str:
    return f"""
        SELECT r.rowid AS physical_rowid,
               ?::VARCHAR AS match_type,{_normal_projection("r")}
        FROM records r
        WHERE r.rowid=? AND r.record_id=?::UINTEGER
    """


def _documents(
    locators: Sequence[tuple[int, int]],
    *,
    match_type: str,
    exact_ids: Sequence[str] = (),
    metrics: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    if not locators:
        return []
    # Physical locations are trusted only while the verified source/sidecar
    # identity pair remains unchanged.
    ensure_ready()
    unique: dict[int, int] = {}
    for record_id, row_location in locators:
        previous = unique.setdefault(int(record_id), int(row_location))
        if previous != int(row_location):
            raise SidecarUnavailable("record locator has conflicting locations")
    documents: list[dict[str, Any]] = []
    with _connection(DUCKDB_PATH) as connection:
        if metrics is not None:
            metrics["payload_requested_ids"] = float(len(unique))
            metrics["payload_rowgroups"] = float(len(unique))
            metrics["widest_payload_range"] = 1.0
        sql = physical_rowid_query_sql()
        for record_id, row_location in sorted(unique.items()):
            cursor = connection.execute(
                sql,
                [match_type, row_location, record_id],
            )
            names = [str(item[0]) for item in cursor.description]
            expected_names = ("physical_rowid", *OUTPUT_COLUMNS)
            if tuple(names) != expected_names:
                raise RuntimeError(
                    "unexpected physical-rowid payload columns"
                )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise SidecarUnavailable(
                    "physical record locator did not resolve exactly one row"
                )
            physical_rowid, *payload = rows[0]
            document = dict(zip(OUTPUT_COLUMNS, payload, strict=True))
            if (
                int(physical_rowid) != row_location
                or int(document["record_id"]) != record_id
            ):
                raise SidecarUnavailable(
                    "physical record locator verification failed"
                )
            if exact_ids and str(document.get("id")) not in exact_ids:
                continue
            documents.append(document)
    documents.sort(key=lambda item: int(item["record_id"]))
    if metrics is not None:
        metrics["payload_rows_returned"] = float(len(documents))
    return documents


def direct_phone(candidates: Sequence[str], *, limit: int | None = None) -> list[dict[str, Any]]:
    ensure_ready()
    normalized = tuple(dict.fromkeys(value for value in candidates if value))
    if not normalized:
        raise ValueError("at least one normalized phone candidate is required")
    maximum = int(limit or MAX_IN_MEMORY_RESULTS) + 1
    record_ids = _phone_record_ids(normalized, maximum)
    return _documents(
        _locators(record_ids),
        match_type="DIRECT",
    )


def phone_search(
    candidates: Sequence[str],
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float]]:
    ensure_ready()
    normalized = tuple(dict.fromkeys(value for value in candidates if value))
    if not normalized:
        raise ValueError("at least one normalized phone candidate is required")
    maximum = int(limit or MAX_IN_MEMORY_RESULTS) + 1
    timings: dict[str, float] = {}

    started = time.monotonic()
    direct_ids = _phone_record_ids(normalized, maximum)
    timings["phone_sidecar_lookup"] = time.monotonic() - started

    started = time.monotonic()
    direct_locators = _locators(direct_ids)
    timings["direct_locator_lookup"] = time.monotonic() - started

    started = time.monotonic()
    direct_metrics: dict[str, float] = {}
    direct = _documents(
        direct_locators,
        match_type="DIRECT",
        metrics=direct_metrics,
    )
    direct_payload_seconds = time.monotonic() - started
    timings["direct_payload_fetch"] = direct_payload_seconds

    started = time.monotonic()
    safe_ids = tuple(
        dict.fromkeys(
            str(item["id"])
            for item in direct
            if item.get("id") is not None
            and str(item["id"]).isascii()
            and str(item["id"]).isdecimal()
            and len(str(item["id"])) == 12
        )
    )
    timings["id_extraction"] = time.monotonic() - started

    related: list[dict[str, Any]] = []
    related_metrics: dict[str, float] = {}
    timings["relationship_sidecar_lookup"] = 0.0
    timings["related_locator_lookup"] = 0.0
    timings["related_payload_fetch"] = 0.0
    if safe_ids:
        started = time.monotonic()
        candidate_ids = _id_record_ids(
            safe_ids,
            maximum + len(direct_ids),
        )
        direct_id_set = {int(record_id) for record_id in direct_ids}
        related_ids = [
            record_id
            for record_id in candidate_ids
            if record_id not in direct_id_set
        ][:maximum]
        timings["relationship_sidecar_lookup"] = (
            time.monotonic() - started
        )

        started = time.monotonic()
        related_locators = _locators(related_ids)
        timings["related_locator_lookup"] = time.monotonic() - started

        started = time.monotonic()
        related = _documents(
            related_locators,
            match_type="RELATED",
            exact_ids=safe_ids,
            metrics=related_metrics,
        )
        timings["related_payload_fetch"] = time.monotonic() - started

    timings["physical_rowid_payload_fetch"] = (
        direct_payload_seconds + timings["related_payload_fetch"]
    )
    for field in (
        "payload_requested_ids",
        "payload_rowgroups",
        "payload_rows_returned",
    ):
        timings[field] = float(direct_metrics.get(field, 0.0)) + float(
            related_metrics.get(field, 0.0)
        )
    timings["widest_payload_range"] = max(
        float(direct_metrics.get("widest_payload_range", 0.0)),
        float(related_metrics.get("widest_payload_range", 0.0)),
    )
    return (
        sorted(direct, key=lambda item: int(item["record_id"])),
        sorted(related, key=lambda item: int(item["record_id"])),
        timings,
    )


def id_search(
    value: str,
    *,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    ensure_ready()
    id_key(value)
    maximum = int(limit or MAX_IN_MEMORY_RESULTS) + 1
    timings: dict[str, float] = {}
    started = time.monotonic()
    record_ids = _id_record_ids((value,), maximum)
    timings["id_sidecar_lookup"] = time.monotonic() - started
    started = time.monotonic()
    locators = _locators(record_ids)
    timings["id_locator_lookup"] = time.monotonic() - started
    started = time.monotonic()
    documents = _documents(
        locators,
        match_type="DIRECT",
        exact_ids=(value,),
        metrics=timings,
    )
    payload_seconds = time.monotonic() - started
    timings["id_payload_fetch"] = payload_seconds
    timings["physical_rowid_payload_fetch"] = payload_seconds
    return documents, timings


def related_from_direct(
    direct: Sequence[dict[str, Any]], *, limit: int | None = None
) -> list[dict[str, Any]]:
    ensure_ready()
    safe_ids = tuple(
        dict.fromkeys(
            str(item["id"])
            for item in direct
            if item.get("id") is not None
            and str(item["id"]).isascii()
            and str(item["id"]).isdecimal()
            and len(str(item["id"])) == 12
        )
    )
    if not safe_ids:
        return []
    maximum = int(limit or MAX_IN_MEMORY_RESULTS) + len(direct) + 1
    record_ids = _id_record_ids(safe_ids, maximum)
    direct_ids = {int(item["record_id"]) for item in direct}
    record_ids = [record_id for record_id in record_ids if record_id not in direct_ids]
    return _documents(
        _locators(record_ids),
        match_type="RELATED",
        exact_ids=safe_ids,
    )


def exact_id(value: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    documents, _timings = id_search(value, limit=limit)
    return documents
