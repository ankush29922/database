from __future__ import annotations

import csv
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import sidecar_queries
from config import CSV_QUERY_CHUNK_ROWS, MAX_IN_MEMORY_RESULTS


JOINABLE_ID_PATTERN = re.compile(r"[0-9]{12}", re.ASCII)
_QUERY_SEMAPHORE = threading.BoundedSemaphore(1)
_CSV_COLUMNS = (
    "original_lookup", "match_type", "record_id", "record_type", "mobile",
    "name", "fname", "email", "alt", "circle", "id", "address",
    "exception_reason",
)


def _result(documents: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    truncated = len(documents) > limit
    documents = documents[:limit]
    return {
        "direct": [item for item in documents if item["match_type"] == "DIRECT"],
        "related": [item for item in documents if item["match_type"] == "RELATED"],
        "truncated": truncated,
        "loaded_records": len(documents),
    }


def clear_cache() -> None:
    return None


def cache_size() -> int:
    return 0


def search_phone(candidates: Sequence[str]) -> dict[str, Any]:
    started = time.monotonic()
    normalized = tuple(dict.fromkeys(value for value in candidates if value))
    with _QUERY_SEMAPHORE:
        direct, related, timings = sidecar_queries.phone_search(
            normalized, limit=MAX_IN_MEMORY_RESULTS
        )
    result = _result([*direct, *related], limit=MAX_IN_MEMORY_RESULTS)
    result["timings"] = {**timings, "database_total": time.monotonic() - started}
    return result


def search_phone_direct(candidates: Sequence[str]) -> dict[str, Any]:
    normalized = tuple(dict.fromkeys(value for value in candidates if value))
    with _QUERY_SEMAPHORE:
        documents = sidecar_queries.direct_phone(normalized, limit=MAX_IN_MEMORY_RESULTS)
    return _result(documents, limit=MAX_IN_MEMORY_RESULTS)


def search_phone_related(direct: Sequence[dict[str, Any]]) -> dict[str, Any]:
    with _QUERY_SEMAPHORE:
        documents = sidecar_queries.related_from_direct(
            direct, limit=MAX_IN_MEMORY_RESULTS
        )
    return _result(documents, limit=MAX_IN_MEMORY_RESULTS)


def search_id(id_value: str) -> dict[str, Any]:
    started = time.monotonic()
    if not JOINABLE_ID_PATTERN.fullmatch(id_value):
        raise ValueError("ID must be exactly 12 ASCII decimal digits")
    with _QUERY_SEMAPHORE:
        documents, timings = sidecar_queries.id_search(
            id_value, limit=MAX_IN_MEMORY_RESULTS
        )
    result = _result(documents, limit=MAX_IN_MEMORY_RESULTS)
    result["timings"] = {**timings, "database_total": time.monotonic() - started}
    return result


def _write_documents(
    writer: csv.DictWriter, original: str, documents: Sequence[dict[str, Any]]
) -> int:
    for document in documents:
        writer.writerow({
            "original_lookup": original,
            **{key: document.get(key) for key in _CSV_COLUMNS[1:]},
        })
    return len(documents)


def export_phone_results(
    original_lookup: str, candidates: Sequence[str], output_path: Path
) -> int:
    result = search_phone(candidates)
    documents = [*result["direct"], *result["related"]]
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        return _write_documents(writer, original_lookup, documents)


def export_id_results(id_value: str, output_path: Path) -> int:
    result = search_id(id_value)
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        return _write_documents(writer, id_value, result["direct"])


def export_phone_batch(
    lookups: Iterable[tuple[str, Sequence[str] | None]],
    output_path: Path,
    *,
    chunk_rows: int = CSV_QUERY_CHUNK_ROWS,
) -> tuple[int, int]:
    items = list(lookups)
    query_count = result_count = 0
    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for offset in range(0, len(items), max(1, chunk_rows)):
            for original, candidates in items[offset : offset + chunk_rows]:
                query_count += 1
                documents: list[dict[str, Any]] = []
                if candidates:
                    result = search_phone(candidates)
                    documents = [*result["direct"], *result["related"]]
                if documents:
                    result_count += _write_documents(writer, original, documents)
                else:
                    writer.writerow({"original_lookup": original})
    return query_count, result_count
