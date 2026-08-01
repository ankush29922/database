#!/usr/bin/env python3
"""Durable, dependency-free Telegram deployment progress notifier."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ENV_PATH = Path("/etc/compactdb/bot.env")
DEPLOYMENT_STATE_PATH = Path("/var/lib/compactdb/deployment-state.json")
NOTIFIER_STATE_PATH = Path("/var/lib/compactdb/notifier-state.json")
TIMER_UNIT = "compactdb-notifier.timer"
UPDATE_SECONDS = 300
STALL_SECONDS = 600
API_TIMEOUT_SECONDS = 15
DOWNLOAD_PHASES = {"DOWNLOADING_GDOWN", "DOWNLOADING_RCLONE", "FALLING_BACK_TO_RCLONE"}
ALERT_STATES = {"STALLED", "STOPPED", "FAILED", "RETRYING"}

PHASE_LABELS = {
    "BOOTSTRAP_STARTED": "Bootstrap started",
    "INSTALLING_PREREQUISITES": "Installing prerequisites",
    "CONFIGURING_SWAP": "Configuring swap",
    "CREATING_VENV": "Creating virtual environment",
    "DOWNLOAD_PREPARATION": "Download preparation",
    "DOWNLOADING_GDOWN": "Downloading with gdown",
    "DOWNLOADING_RCLONE": "Downloading with rclone",
    "FALLING_BACK_TO_RCLONE": "Falling back to rclone",
    "DOWNLOAD_COMPLETE": "Download complete",
    "INSTALLING_APPLICATION": "Installing application",
    "CONFIGURING_PERMISSIONS": "Configuring permissions",
    "CONFIGURING_BOT": "Configuring permissions",
    "STARTING_BOT": "Starting bot",
    "BOT_HEALTHY": "Bot healthy",
    "COMPLETE": "Deployment complete",
    "DEPLOYMENT_FAILED": "Deployment failed",
}


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


def load_environment(path: Path | None = None) -> dict[str, str]:
    path = ENV_PATH if path is None else path
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value.strip().strip("'\"")
    return values


def owner_ids(environment: dict[str, str]) -> list[str]:
    values = [
        item.strip()
        for item in environment.get("BOT_OWNER_IDS", "").split(",")
        if item.strip().isascii() and item.strip().isdecimal()
    ]
    return list(dict.fromkeys(values))


def clean(value: Any, default: str = "-") -> str:
    text = str(value if value not in (None, "") else default)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:120] or default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def percentage(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def human_bytes(value: Any) -> str:
    amount = max(0, integer(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    number = float(amount)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{amount} B"
        number /= 1024
    return f"{amount} B"


def duration(seconds: Any) -> str:
    remaining = max(0, integer(seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def eta(value: Any) -> str:
    return "UNKNOWN" if value is None or integer(value, -1) < 0 else duration(value)


def parse_timestamp(value: Any) -> int:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return 0


def elapsed_seconds(deployment: dict[str, Any], now: int) -> int:
    started = parse_timestamp(deployment.get("deployment_start_time"))
    completed = parse_timestamp(deployment.get("deployment_completion_time"))
    return max(0, (completed or now) - started) if started else 0


def phase_label(deployment: dict[str, Any]) -> str:
    phase = clean(deployment.get("current_phase"), "BOOTSTRAP_STARTED")
    return PHASE_LABELS.get(phase, phase.replace("_", " ").title())


def is_failed(deployment: dict[str, Any]) -> bool:
    return clean(deployment.get("current_phase"), "") == "DEPLOYMENT_FAILED"


def is_complete(deployment: dict[str, Any]) -> bool:
    return clean(deployment.get("current_phase"), "") == "COMPLETE"


def sanitize_detail(value: Any) -> str:
    text = clean(value, "Unknown failure")
    text = re.sub(r"https?://\S+", "[redacted URL]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(bot_token|telegram_token|google_token|oauth_token|access_token|refresh_token|token|password|passwd|secret|authorization|cookie|oauth|client_secret)\b\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"(?i)bot\d{5,}:[A-Za-z0-9_-]+", "bot[redacted]", text)
    return clean(text, "Unknown failure")


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def download_health(
    deployment: dict[str, Any],
    now: int,
    process_checker: Callable[[int], bool] = process_exists,
) -> str:
    phase = clean(deployment.get("current_phase"), "")
    process_state = clean(deployment.get("downloader_process_state"), "").upper()
    if is_failed(deployment):
        return "FAILED"
    if phase not in DOWNLOAD_PHASES:
        return "COMPLETE" if is_complete(deployment) else "IDLE"
    if process_state == "FAILED":
        return "FAILED"
    if process_state == "RETRYING":
        return "RETRYING"
    if process_state in {"PREPARING", "EXITED_SUCCESS"}:
        return process_state
    pid = integer(deployment.get("downloader_pid"))
    if not process_checker(pid):
        return "STOPPED"
    growth_epoch = parse_timestamp(deployment.get("last_successful_byte_growth_time"))
    monitoring_epoch = parse_timestamp(deployment.get("download_monitoring_start_time"))
    reference = growth_epoch or monitoring_epoch
    if reference and now - reference >= STALL_SECONDS:
        return "STALLED"
    return "RUNNING"


def progress_message(
    deployment: dict[str, Any],
    notifier_state: dict[str, Any],
    now: int,
    initial: bool = False,
) -> str:
    title = "🚀 CompactDB VPS deployment started" if initial else "🚀 CompactDB VPS deployment in progress"
    swap_total = integer(deployment.get("swap_total"))
    swap_used = integer(deployment.get("swap_used"))
    return "\n".join(
        (
            title,
            "",
            f"VPS: {clean(deployment.get('hostname'), socket.gethostname())}",
            f"Started: {clean(deployment.get('deployment_start_time'))}",
            f"Phase: {phase_label(deployment)}",
            f"Download method: {clean(deployment.get('downloader_name') or deployment.get('selected_download_method') or deployment.get('configured_download_method'))}",
            f"Current file: {clean(deployment.get('current_file'), 'NONE')}",
            f"Current file bytes: {human_bytes(deployment.get('current_file_bytes'))} / {human_bytes(deployment.get('current_file_expected_bytes'))}",
            f"Files completed: {integer(deployment.get('completed_files'))} / {integer(deployment.get('remote_package_files'), 29)}",
            f"Total bytes: {integer(deployment.get('downloaded_bytes'))} / {integer(deployment.get('total_bytes'))}",
            f"Progress: {percentage(deployment.get('percentage')):.1f}%",
            f"Current 60-second speed: {human_bytes(deployment.get('current_rate'))}/s",
            f"Session average speed: {human_bytes(deployment.get('average_rate'))}/s",
            f"ETA: {eta(deployment.get('eta_seconds'))}",
            f"Last confirmed byte growth: {clean(deployment.get('last_successful_byte_growth_time'), 'NONE')}",
            f"Downloader process: {clean(deployment.get('downloader_process_state'), 'UNKNOWN')} (PID {integer(deployment.get('downloader_pid'))})",
            f"Disk free: {human_bytes(deployment.get('disk_free'))}",
            f"RAM available: {human_bytes(deployment.get('memory_available'))} / {human_bytes(deployment.get('memory_total'))}",
            f"Swap used: {human_bytes(swap_used)} / {human_bytes(swap_total)}",
            f"Elapsed: {duration(elapsed_seconds(deployment, now))}",
            f"Retries: {integer(deployment.get('retries'))}",
            "Main bot: PENDING_DOWNLOAD",
            f"Deployment notifier: {clean(notifier_state.get('delivery_status'), 'starting')}",
        )
    )


def success_message(deployment: dict[str, Any], now: int) -> str:
    return "\n".join(
        (
            "✅ CompactDB deployment complete",
            "",
            f"Total installation time: {duration(elapsed_seconds(deployment, now))}",
            f"Final database package size: {human_bytes(deployment.get('final_package_bytes') or deployment.get('total_bytes'))}",
            f"Final disk free: {human_bytes(deployment.get('disk_free'))}",
            f"Bot ActiveState: {clean(deployment.get('bot_service_state'))}",
            f"Bot MainPID: {integer(deployment.get('bot_main_pid'))}",
            f"Telegram Application started: {clean(deployment.get('telegram_application_started'), 'no')}",
            "Diagnostic command: compactdb status",
        )
    )


def failure_message(deployment: dict[str, Any]) -> str:
    category = clean(deployment.get("last_error_class"), "DEPLOYMENT_FAILED")
    category = re.sub(r"[^A-Z0-9_ -]", "", category.upper())[:80] or "DEPLOYMENT_FAILED"
    return "\n".join(
        (
            "❌ CompactDB deployment requires attention",
            "",
            f"Failed phase: {clean(deployment.get('failed_phase'), 'unknown')}",
            f"Error category: {category}",
            f"Reason: {sanitize_detail(deployment.get('sanitized_error_detail'))}",
            f"Retry count: {integer(deployment.get('retries'))}",
            "Resumable: yes",
            "Command: compactdb repair",
        )
    )


def alert_message(status_value: str, deployment: dict[str, Any]) -> str:
    method = clean(deployment.get("downloader_name"), "unknown")
    lines = [f"⚠️ CompactDB download {status_value}", f"Downloader: {method}"]
    if status_value == "FAILED":
        lines.extend(
            (
                f"Exit code: {integer(deployment.get('downloader_exit_code'), -1)}",
                f"Reason: {sanitize_detail(deployment.get('sanitized_error_detail'))}",
            )
        )
    elif status_value == "RETRYING":
        lines.append(f"Retry count: {integer(deployment.get('retries'))}")
    elif status_value == "RECOVERED":
        lines.append("Confirmed package-byte growth has resumed.")
    lines.append(f"Recovery action: {clean(deployment.get('recovery_action'), 'automatic resume/repair')}")
    return "\n".join(lines)


class TelegramAPI:
    def __init__(self, token: str, opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.token = token
        self.opener = opener

    def call(self, method: str, values: dict[str, str]) -> dict[str, Any] | None:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=urllib.parse.urlencode(values).encode("utf-8"),
            method="POST",
        )
        try:
            with self.opener(request, timeout=API_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
                return None
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
        except Exception:
            return None

    def send(self, chat_id: str, text: str) -> int | None:
        result = self.call("sendMessage", {"chat_id": chat_id, "text": text})
        message_id = integer(result.get("message_id")) if result is not None else 0
        return message_id or None

    def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        result = self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": str(message_id), "text": text},
        )
        return result is not None


def should_deliver(deployment: dict[str, Any], state: dict[str, Any], now: int, force: bool) -> bool:
    if force or state.get("pending") or not state.get("messages"):
        return True
    if is_complete(deployment) and not state.get("final_delivered"):
        return True
    phase = clean(deployment.get("current_phase"), "BOOTSTRAP_STARTED")
    if phase != state.get("last_phase"):
        return True
    if percentage(deployment.get("percentage")) >= percentage(state.get("last_percentage")) + 1.0:
        return True
    if integer(deployment.get("retries")) != integer(state.get("last_retries")):
        return True
    if clean(deployment.get("last_error_class"), "") != clean(state.get("last_error_class"), ""):
        return True
    if clean(deployment.get("downloader_process_state"), "") != clean(state.get("last_process_state"), ""):
        return True
    return now - integer(state.get("last_delivered_at")) >= UPDATE_SECONDS


def stop_timer() -> None:
    try:
        subprocess.run(
            ("/usr/bin/systemctl", "stop", TIMER_UNIT),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass


def deliver(
    force: bool = False,
    now: int | None = None,
    api_factory: Callable[[str], TelegramAPI] = TelegramAPI,
    process_checker: Callable[[int], bool] = process_exists,
) -> bool:
    current_time = int(time.time()) if now is None else now
    deployment = load_json(DEPLOYMENT_STATE_PATH)
    notifier = load_json(NOTIFIER_STATE_PATH)
    if is_complete(deployment) and notifier.get("final_delivered") and not notifier.get("pending"):
        notifier["delivery_status"] = "final_delivered"
        atomic_json(NOTIFIER_STATE_PATH, notifier)
        stop_timer()
        return True
    environment = load_environment()
    token = environment.get("BOT_TOKEN", "").strip()
    chats = owner_ids(environment)
    notifier["last_attempt_at"] = current_time
    if not token or not chats or not deployment:
        notifier["delivery_status"] = "waiting_for_configuration_or_state"
        notifier["pending"] = True
        atomic_json(NOTIFIER_STATE_PATH, notifier)
        return False

    health = download_health(deployment, current_time, process_checker)
    active_alert = clean(notifier.get("active_alert"), "")
    pending_alert = notifier.get("pending_alert")
    if not isinstance(pending_alert, dict):
        pending_alert = {}
    if is_complete(deployment):
        pending_alert = {}
        notifier["active_alert"] = ""
        active_alert = ""
    downloaded = integer(deployment.get("downloaded_bytes"))
    if health in ALERT_STATES and health != active_alert:
        pending_alert = {
            "status": health,
            "text": alert_message(health, deployment),
            "created_at": current_time,
            "downloaded_bytes": downloaded,
            "delivered_chats": [],
        }
        notifier["active_alert"] = health
        notifier["alert_downloaded_bytes"] = downloaded
    elif (
        active_alert in {"STALLED", "STOPPED", "FAILED", "RETRYING"}
        and health == "RUNNING"
        and downloaded > integer(notifier.get("alert_downloaded_bytes"))
        and not pending_alert
    ):
        pending_alert = {
            "status": "RECOVERED",
            "text": alert_message("RECOVERED", deployment),
            "created_at": current_time,
            "downloaded_bytes": downloaded,
            "delivered_chats": [],
        }
        notifier["active_alert"] = ""
    notifier["download_health"] = health
    notifier["pending_alert"] = pending_alert

    alert_due = bool(pending_alert)
    if not should_deliver(deployment, notifier, current_time, force or alert_due):
        notifier["delivery_status"] = "throttled"
        atomic_json(NOTIFIER_STATE_PATH, notifier)
        return True

    failed = is_failed(deployment)
    complete = is_complete(deployment) and not failed
    messages = notifier.get("messages")
    if not isinstance(messages, dict):
        messages = {}
    text = (
        failure_message(deployment)
        if failed
        else success_message(deployment, current_time)
        if complete
        else progress_message(deployment, notifier, current_time, initial=not bool(messages))
    )
    api = api_factory(token)
    all_delivered = True
    active_chats = set(chats)
    messages = {key: value for key, value in messages.items() if key in active_chats}
    for chat_id in chats:
        message_id = integer(messages.get(chat_id))
        if message_id:
            delivered = api.edit(chat_id, message_id, text)
        else:
            new_message_id = api.send(chat_id, text)
            delivered = new_message_id is not None
            if new_message_id is not None:
                messages[chat_id] = new_message_id
        all_delivered = all_delivered and delivered

    if pending_alert:
        delivered_chats = {
            str(item) for item in pending_alert.get("delivered_chats", []) if str(item) in active_chats
        }
        for chat_id in chats:
            if chat_id in delivered_chats:
                continue
            alert_text = str(pending_alert.get("text") or "CompactDB download alert")[:4000]
            if api.send(chat_id, alert_text) is not None:
                delivered_chats.add(chat_id)
        pending_alert["delivered_chats"] = sorted(delivered_chats)
        if delivered_chats == active_chats:
            notifier["last_alert_status"] = clean(pending_alert.get("status"), "")
            notifier["last_alert_delivered_at"] = current_time
            pending_alert = {}
        else:
            all_delivered = False

    notifier["messages"] = messages
    notifier["message_count"] = len(messages)
    notifier["pending_alert"] = pending_alert
    notifier["pending"] = not all_delivered or bool(pending_alert)
    notifier["delivery_status"] = "delivered" if all_delivered else "queued_for_retry"
    if all_delivered:
        notifier["last_delivered_at"] = current_time
        notifier["last_phase"] = clean(deployment.get("current_phase"), "BOOTSTRAP_STARTED")
        notifier["last_percentage"] = percentage(deployment.get("percentage"))
        notifier["last_retries"] = integer(deployment.get("retries"))
        notifier["last_error_class"] = clean(deployment.get("last_error_class"), "")
        notifier["last_process_state"] = clean(deployment.get("downloader_process_state"), "")
        if complete:
            notifier["final_delivered"] = True
    atomic_json(NOTIFIER_STATE_PATH, notifier)
    if all_delivered and complete:
        stop_timer()
    return all_delivered


def send_manual_test(api_factory: Callable[[str], TelegramAPI] = TelegramAPI) -> bool:
    environment = load_environment()
    token = environment.get("BOT_TOKEN", "").strip()
    chats = owner_ids(environment)
    if not token or not chats:
        return False
    api = api_factory(token)
    text = "CompactDB notifier manual test. No deployment state was changed."
    results = [api.send(chat_id, text) is not None for chat_id in chats]
    return bool(results) and all(results)


def status() -> None:
    state = load_json(NOTIFIER_STATE_PATH)
    print(f"Notifier delivery: {clean(state.get('delivery_status'), 'not-started')}")
    print(f"Notifier pending: {'yes' if state.get('pending') else 'no'}")
    print(f"Notifier messages: {integer(state.get('message_count'))}")
    print(f"Notifier phase: {clean(state.get('last_phase'))}")
    print(f"Notifier last attempt: {integer(state.get('last_attempt_at'))}")
    print(f"Notifier last delivery: {integer(state.get('last_delivered_at'))}")
    print(f"Notifier final delivered: {'yes' if state.get('final_delivered') else 'no'}")
    print(f"Download health: {clean(state.get('download_health'), 'unknown')}")
    print(f"Active download alert: {clean(state.get('active_alert'), 'none')}")


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    command = arguments[0] if arguments else "update"
    if command == "status":
        status()
    elif command == "test":
        print(f"NOTIFIER_TEST={'DELIVERED' if send_manual_test() else 'NOT_DELIVERED'}")
    elif command in {"update", "retry", "deployment-complete", "bot-started"}:
        deliver(force=command in {"deployment-complete", "bot-started"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
