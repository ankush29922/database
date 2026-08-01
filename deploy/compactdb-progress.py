#!/usr/bin/env python3
"""Metadata-only, durable CompactDB download progress accounting."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_FILES = 29
EXPECTED_BYTES = 220754442143
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".download", ".crdownload")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def safe_manifest_path(raw: Any) -> str:
    value = str(raw or "").replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("remote manifest contains an unsafe path")
    return str(path)


def normalize_rclone_manifest(
    raw: Any,
    expected_files: int = EXPECTED_FILES,
    expected_bytes: int = EXPECTED_BYTES,
) -> dict[str, Any]:
    if not isinstance(raw, list):
        raise ValueError("rclone manifest is not a list")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("IsDir") is True:
            continue
        path = safe_manifest_path(item.get("Path"))
        size = int(item.get("Size", -1))
        if size < 0 or path in seen:
            raise ValueError("remote manifest contains invalid file metadata")
        seen.add(path)
        files.append({"path": path, "size": size})
    files.sort(key=lambda item: str(item["path"]))
    total = sum(int(item["size"]) for item in files)
    if len(files) != expected_files or total != expected_bytes:
        raise ValueError("remote manifest count or byte total differs from the audited package")
    return {
        "version": 1,
        "file_count": len(files),
        "total_bytes": total,
        "files": files,
    }


def write_normalized_manifest(
    source: Path,
    destination: Path,
    expected_files: int = EXPECTED_FILES,
    expected_bytes: int = EXPECTED_BYTES,
) -> dict[str, Any]:
    raw = json.loads(source.read_text(encoding="utf-8"))
    manifest = normalize_rclone_manifest(raw, expected_files, expected_bytes)
    atomic_json(destination, manifest)
    return manifest


def validated_manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("authoritative manifest is unavailable")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("authoritative manifest contains invalid entries")
        name = safe_manifest_path(item.get("path"))
        size = int(item.get("size", -1))
        if size < 0 or name in seen:
            raise ValueError("authoritative manifest contains invalid entries")
        seen.add(name)
        normalized.append({"path": name, "size": size})
    total = sum(int(item["size"]) for item in normalized)
    declared_count = int(manifest.get("file_count", len(normalized)))
    declared_total = int(manifest.get("total_bytes", total))
    if declared_count != len(normalized) or declared_total != total:
        raise ValueError("authoritative manifest totals are inconsistent")
    return {"version": 1, "file_count": len(normalized), "total_bytes": total, "files": normalized}


def _partial_match(candidate: str, basename: str) -> bool:
    lowered = candidate.lower()
    if not lowered.endswith(PARTIAL_SUFFIXES):
        return False
    return candidate.startswith(basename) or candidate.startswith(f".{basename}")


def _stat_size(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return max(0, int(metadata.st_size)), int(metadata.st_mtime_ns)


def scan_package(package: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    entries = list(manifest["files"])
    by_directory: dict[PurePosixPath, list[dict[str, Any]]] = {}
    for item in entries:
        relative = PurePosixPath(str(item["path"]))
        by_directory.setdefault(relative.parent, []).append(item)

    partials: dict[str, list[tuple[str, int, int]]] = {str(item["path"]): [] for item in entries}
    for relative_directory, expected in by_directory.items():
        directory = package.joinpath(*relative_directory.parts)
        try:
            children = list(os.scandir(directory))
        except OSError:
            children = []
        expected_basenames = {PurePosixPath(str(item["path"])).name: item for item in expected}
        for child in children:
            if not child.is_file(follow_symlinks=False) or child.name in expected_basenames:
                continue
            matches = [name for name in expected_basenames if _partial_match(child.name, name)]
            if not matches:
                continue
            owner = max(matches, key=len)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError:
                continue
            owner_path = str(expected_basenames[owner]["path"])
            child_relative = str(relative_directory / child.name)
            partials[owner_path].append((child_relative, max(0, int(metadata.st_size)), int(metadata.st_mtime_ns)))

    raw_bytes = 0
    completed_files = 0
    active: list[tuple[int, int, str, int, int]] = []
    for item in entries:
        relative = str(item["path"])
        expected_size = int(item["size"])
        final_metadata = _stat_size(package.joinpath(*PurePosixPath(relative).parts))
        final_size, final_mtime = final_metadata or (0, 0)
        complete = final_metadata is not None and final_size == expected_size
        if complete:
            completed_files += 1
        candidates = [(relative, final_size, final_mtime)] if final_metadata is not None else []
        candidates.extend(partials[relative])
        if candidates:
            selected_size = max(candidates, key=lambda value: (value[1], value[2]))[1]
            counted = min(expected_size, max(0, selected_size))
        else:
            counted = 0
        raw_bytes += counted
        if not complete and candidates:
            active_name, active_size, active_mtime = max(candidates, key=lambda value: (value[2], value[1]))
            active_counted = min(expected_size, max(0, active_size))
            if active_counted > 0:
                active.append((active_mtime, active_counted, active_name, active_counted, expected_size))

    current = max(active, default=(0, 0, "", 0, 0), key=lambda value: (value[0], value[1]))
    return {
        "raw_downloaded_bytes": min(int(manifest["total_bytes"]), raw_bytes),
        "completed_files": completed_files,
        "current_file": current[2],
        "current_file_bytes": current[3],
        "current_file_expected_bytes": current[4],
    }


def _iso_time(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_progress(
    package: Path,
    manifest_path: Path,
    history_path: Path,
    deployment_state_path: Path,
    downloader: str,
    pid: int,
    process_state: str,
    now_epoch: float,
    now_monotonic: float,
    boot_id: str,
    expected_files: int | None = None,
    expected_bytes: int | None = None,
) -> dict[str, Any]:
    manifest = validated_manifest(manifest_path)
    if expected_files is not None and int(manifest["file_count"]) != expected_files:
        raise ValueError("authoritative manifest file count differs from the audited package")
    if expected_bytes is not None and int(manifest["total_bytes"]) != expected_bytes:
        raise ValueError("authoritative manifest byte total differs from the audited package")
    observed = scan_package(package, manifest)
    history = load_json(history_path)
    previous_bytes = min(int(manifest["total_bytes"]), int(history.get("max_downloaded_bytes", 0)))
    effective_bytes = min(
        int(manifest["total_bytes"]),
        max(previous_bytes, int(observed["raw_downloaded_bytes"])),
    )
    grew = effective_bytes > previous_bytes
    monitoring_started = str(history.get("monitoring_started_timestamp") or "")
    if not monitoring_started:
        monitoring_started = _iso_time(now_epoch)
    last_growth = str(history.get("last_growth_timestamp") or "")
    if grew:
        last_growth = _iso_time(now_epoch)

    previous_boot = str(history.get("boot_id") or "")
    samples = history.get("samples") if previous_boot == boot_id else []
    if not isinstance(samples, list):
        samples = []
    cleaned_samples: list[dict[str, float | int]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        sample_time = float(sample.get("monotonic", -1))
        sample_bytes = int(sample.get("bytes", 0))
        if 0 <= sample_time <= now_monotonic:
            cleaned_samples.append({"monotonic": sample_time, "bytes": sample_bytes})
    prior_sample = cleaned_samples[-1] if cleaned_samples else None
    cleaned_samples.append({"monotonic": now_monotonic, "bytes": effective_bytes})
    cutoff = now_monotonic - 60.0
    before_cutoff = [sample for sample in cleaned_samples if float(sample["monotonic"]) <= cutoff]
    within_window = [sample for sample in cleaned_samples if float(sample["monotonic"]) > cutoff]
    rolling_samples = ([before_cutoff[-1]] if before_cutoff else []) + within_window
    if prior_sample is None or effective_bytes <= int(prior_sample["bytes"]):
        current_rate = 0
    else:
        anchor = rolling_samples[0]
        elapsed = now_monotonic - float(anchor["monotonic"])
        current_rate = int((effective_bytes - int(anchor["bytes"])) / elapsed) if elapsed > 0 else 0

    session_started_epoch = float(history.get("session_started_epoch", now_epoch))
    session_start_bytes = int(history.get("session_start_bytes", previous_bytes))
    if "session_started_epoch" not in history:
        session_started_epoch = now_epoch
        session_start_bytes = previous_bytes
    session_elapsed = max(0.0, now_epoch - session_started_epoch)
    average_rate = (
        int((effective_bytes - session_start_bytes) / session_elapsed)
        if session_elapsed > 0 and effective_bytes >= session_start_bytes
        else 0
    )
    remaining = max(0, int(manifest["total_bytes"]) - effective_bytes)
    eta_seconds = int(remaining / current_rate) if current_rate > 0 and remaining > 0 else None
    percentage = min(100.0, max(0.0, 100.0 * effective_bytes / int(manifest["total_bytes"])))

    history.update(
        {
            "version": 1,
            "boot_id": boot_id,
            "max_downloaded_bytes": effective_bytes,
            "monitoring_started_timestamp": monitoring_started,
            "last_growth_timestamp": last_growth or None,
            "samples": rolling_samples,
            "session_started_epoch": session_started_epoch,
            "session_start_bytes": session_start_bytes,
        }
    )
    atomic_json(history_path, history)

    telemetry = {
        "downloaded_bytes": effective_bytes,
        "total_bytes": int(manifest["total_bytes"]),
        "percentage": round(percentage, 4),
        "completed_files": int(observed["completed_files"]),
        "remote_package_files": int(manifest["file_count"]),
        "current_file": observed["current_file"],
        "current_file_bytes": int(observed["current_file_bytes"]),
        "current_file_expected_bytes": int(observed["current_file_expected_bytes"]),
        "current_rate": current_rate,
        "average_rate": average_rate,
        "eta_seconds": eta_seconds,
        "last_successful_byte_growth_time": last_growth or None,
        "download_monitoring_start_time": monitoring_started,
        "downloader_name": downloader,
        "downloader_pid": max(0, int(pid)),
        "downloader_process_state": process_state,
    }
    deployment = load_json(deployment_state_path)
    deployment.update(telemetry)
    deployment["last_progress_timestamp"] = _iso_time(now_epoch)
    atomic_json(deployment_state_path, deployment)
    return telemetry


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return "unknown-boot"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--input", type=Path, required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--expected-files", type=int, default=EXPECTED_FILES)
    manifest_parser.add_argument("--expected-bytes", type=int, default=EXPECTED_BYTES)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--package", type=Path, required=True)
    snapshot_parser.add_argument("--manifest", type=Path, required=True)
    snapshot_parser.add_argument("--history", type=Path, required=True)
    snapshot_parser.add_argument("--state", type=Path, required=True)
    snapshot_parser.add_argument("--downloader", choices=("gdown", "rclone"), required=True)
    snapshot_parser.add_argument("--pid", type=int, required=True)
    snapshot_parser.add_argument("--process-state", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "manifest":
        manifest = write_normalized_manifest(
            arguments.input,
            arguments.output,
            arguments.expected_files,
            arguments.expected_bytes,
        )
        print(f"{manifest['file_count']} {manifest['total_bytes']}")
        return 0
    update_progress(
        arguments.package,
        arguments.manifest,
        arguments.history,
        arguments.state,
        arguments.downloader,
        arguments.pid,
        arguments.process_state,
        now_epoch=dt.datetime.now(tz=dt.timezone.utc).timestamp(),
        now_monotonic=__import__("time").monotonic(),
        boot_id=boot_id(),
        expected_files=EXPECTED_FILES,
        expected_bytes=EXPECTED_BYTES,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
