from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from config import SETTINGS_FILE


_LOCK = threading.RLock()


def _read(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"enabled": True}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read local bot settings: {exc}") from exc
    if set(data) != {"enabled"} or not isinstance(data["enabled"], bool):
        raise RuntimeError("local bot settings must contain only a boolean enabled field")
    return data


def is_enabled(path: Path = SETTINGS_FILE) -> bool:
    with _LOCK:
        return bool(_read(path)["enabled"])


def set_enabled(enabled: bool, path: Path = SETTINGS_FILE) -> bool:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        payload = json.dumps({"enabled": bool(enabled)}, sort_keys=True) + "\n"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return bool(enabled)
