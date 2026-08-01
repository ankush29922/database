#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "compactdb_telegram_notifier",
    REPOSITORY / "deploy" / "compactdb-telegram-notifier.py",
)
assert SPEC and SPEC.loader
notifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifier)


class MockTelegram:
    def __init__(self, send_result: int | None = 7001, edit_result: bool = True) -> None:
        self.send_result = send_result
        self.edit_result = edit_result
        self.calls: list[tuple[object, ...]] = []
        self.factory_calls = 0

    def __call__(self, token: str) -> "MockTelegram":
        self.factory_calls += 1
        self.calls.append(("configured", bool(token)))
        return self

    def send(self, chat_id: str, text: str) -> int | None:
        self.calls.append(("send", chat_id, text))
        return self.send_result

    def edit(self, chat_id: str, message_id: int, text: str) -> bool:
        self.calls.append(("edit", chat_id, message_id, text))
        return self.edit_result


class NotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.environment_path = root / "bot.env"
        self.deployment_path = root / "deployment-state.json"
        self.notifier_path = root / "notifier-state.json"
        self.environment_path.write_text("BOT_TOKEN='mock-secret'\nBOT_OWNER_IDS='1001'\n", encoding="utf-8")
        notifier.ENV_PATH = self.environment_path
        notifier.DEPLOYMENT_STATE_PATH = self.deployment_path
        notifier.NOTIFIER_STATE_PATH = self.notifier_path
        self.timer_stops = 0
        self.stop_patch = mock.patch.object(notifier, "stop_timer", side_effect=self._stop_timer)
        self.stop_patch.start()
        self.write_deployment()

    def tearDown(self) -> None:
        self.stop_patch.stop()
        self.temporary.cleanup()

    def _stop_timer(self) -> None:
        self.timer_stops += 1

    def write_deployment(self, **changes: object) -> None:
        state: dict[str, object] = {
            "current_phase": "BOOTSTRAP_STARTED",
            "deployment_start_time": "2026-08-01T00:00:00Z",
            "hostname": "mock-vps",
            "percentage": 0,
            "downloaded_bytes": 0,
            "total_bytes": 1000,
            "current_rate": 0,
            "average_rate": 0,
            "eta_seconds": 100,
            "completed_files": 0,
            "remote_package_files": 29,
            "disk_total": 2000,
            "disk_free": 1500,
            "memory_total": 1000,
            "memory_available": 800,
            "swap_total": 500,
            "swap_used": 10,
            "retries": 0,
            "configured_download_method": "gdown",
            "bot_service_state": "inactive",
            "downloader_name": "gdown",
            "downloader_pid": 4242,
            "downloader_process_state": "RUNNING",
            "download_monitoring_start_time": "2026-08-01T00:00:00Z",
        }
        if self.deployment_path.exists():
            state.update(json.loads(self.deployment_path.read_text(encoding="utf-8")))
        state.update(changes)
        self.deployment_path.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict[str, object]:
        return json.loads(self.notifier_path.read_text(encoding="utf-8"))

    def test_initial_message_and_same_message_edit(self) -> None:
        telegram = MockTelegram()
        self.assertTrue(notifier.deliver(now=1785542400, api_factory=telegram))
        send = [call for call in telegram.calls if call[0] == "send"]
        self.assertEqual(len(send), 1)
        self.assertIn("🚀 CompactDB VPS deployment started", send[0][2])
        message_id = self.state()["messages"]["1001"]

        self.write_deployment(current_phase="INSTALLING_PREREQUISITES")
        self.assertTrue(notifier.deliver(now=1785542410, api_factory=telegram))
        edits = [call for call in telegram.calls if call[0] == "edit"]
        self.assertEqual(edits[-1][2], message_id)
        self.assertIn("Installing prerequisites", edits[-1][3])

    def test_one_percent_and_five_minute_throttling(self) -> None:
        telegram = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=telegram)
        baseline = len(telegram.calls)
        self.write_deployment(percentage=0.9)
        notifier.deliver(now=1785542410, api_factory=telegram)
        self.assertEqual(len(telegram.calls), baseline)
        self.write_deployment(percentage=1.0)
        notifier.deliver(now=1785542420, api_factory=telegram)
        self.assertGreater(len(telegram.calls), baseline)
        after_percent = len(telegram.calls)
        notifier.deliver(now=1785542721, api_factory=telegram)
        self.assertGreater(len(telegram.calls), after_percent)

    def test_phase_change_is_immediate(self) -> None:
        telegram = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=telegram)
        self.write_deployment(current_phase="CONFIGURING_SWAP")
        notifier.deliver(now=1785542401, api_factory=telegram)
        self.assertIn("Configuring swap", telegram.calls[-1][3])

    def test_retry_count_change_is_immediate(self) -> None:
        telegram = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=telegram)
        baseline = len(telegram.calls)
        self.write_deployment(retries=1)
        notifier.deliver(now=1785542401, api_factory=telegram)
        self.assertGreater(len(telegram.calls), baseline)
        self.assertIn("Retries: 1", telegram.calls[-1][3])

    def test_timeout_is_queued_and_retry_succeeds(self) -> None:
        def timeout_opener(*_args: object, **_kwargs: object) -> object:
            raise TimeoutError

        api = notifier.TelegramAPI("mock-secret", opener=timeout_opener)
        self.assertIsNone(api.send("1001", "mock message"))
        timeout = MockTelegram(send_result=None)
        self.assertFalse(notifier.deliver(now=1785542400, api_factory=timeout))
        self.assertTrue(self.state()["pending"])
        recovered = MockTelegram()
        self.assertTrue(notifier.deliver(now=1785542460, api_factory=recovered))
        self.assertFalse(self.state()["pending"])

    def test_reboot_resume_reuses_durable_message_id(self) -> None:
        before_reboot = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=before_reboot)
        durable_message_id = self.state()["messages"]["1001"]
        self.write_deployment(current_phase="DOWNLOADING_GDOWN", percentage=12)
        after_reboot = MockTelegram()
        notifier.deliver(now=1785542500, api_factory=after_reboot)
        edit = [call for call in after_reboot.calls if call[0] == "edit"][0]
        self.assertEqual(edit[2], durable_message_id)

    def test_success_finalizes_and_stops_timer(self) -> None:
        telegram = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=telegram)
        self.write_deployment(
            current_phase="COMPLETE",
            percentage=100,
            deployment_completion_time="2026-08-01T01:00:00Z",
            final_package_bytes=1000,
            bot_service_state="active",
            bot_main_pid=4321,
            telegram_application_started="yes",
        )
        notifier.deliver(now=1785546000, api_factory=telegram)
        final_text = [call for call in telegram.calls if call[0] == "edit"][-1][3]
        self.assertIn("✅ CompactDB deployment complete", final_text)
        self.assertIn("compactdb status", final_text)
        self.assertTrue(self.state()["final_delivered"])
        self.assertEqual(self.timer_stops, 1)

    def test_failure_is_privacy_safe(self) -> None:
        telegram = MockTelegram()
        notifier.deliver(now=1785542400, api_factory=telegram)
        self.write_deployment(
            current_phase="DEPLOYMENT_FAILED",
            failed_phase="DOWNLOADING_GDOWN",
            last_error_class="GDOWN_TRANSFER_FAILED",
            retries=3,
            error_timestamp="2026-08-01T00:10:00Z",
            final_error_line="BOT_TOKEN=mock-secret owner=1001 private database row",
            sanitized_error_detail="network failure BOT_TOKEN=mock-secret https://user:pass@example.invalid/path",
        )
        notifier.deliver(now=1785543000, api_factory=telegram)
        failure_text = [call for call in telegram.calls if call[0] == "edit"][-1][3]
        self.assertIn("❌ CompactDB deployment requires attention", failure_text)
        self.assertIn("compactdb repair", failure_text)
        self.assertNotIn("mock-secret", failure_text)
        self.assertNotIn("private database row", failure_text)
        self.assertNotIn("user:pass", failure_text)
        self.assertIn("network failure", failure_text)

    def test_stalled_alert_after_ten_minutes(self) -> None:
        telegram = MockTelegram()
        self.write_deployment(
            current_phase="DOWNLOADING_GDOWN",
            last_successful_byte_growth_time="2026-08-01T00:00:00Z",
        )
        notifier.deliver(
            now=1785543060,
            api_factory=telegram,
            process_checker=lambda _pid: True,
        )
        alerts = [call[2] for call in telegram.calls if call[0] == "send"]
        self.assertTrue(any("download STALLED" in text for text in alerts))
        self.assertEqual(self.state()["download_health"], "STALLED")

    def test_stopped_alert_when_downloader_disappears(self) -> None:
        telegram = MockTelegram()
        self.write_deployment(current_phase="DOWNLOADING_GDOWN")
        notifier.deliver(
            now=1785542410,
            api_factory=telegram,
            process_checker=lambda _pid: False,
        )
        alerts = [call[2] for call in telegram.calls if call[0] == "send"]
        self.assertTrue(any("download STOPPED" in text for text in alerts))

    def test_recovered_alert_is_sent_once_after_growth(self) -> None:
        telegram = MockTelegram()
        self.write_deployment(
            current_phase="DOWNLOADING_GDOWN",
            downloaded_bytes=100,
            percentage=10,
            last_successful_byte_growth_time="2026-08-01T00:00:00Z",
        )
        notifier.deliver(now=1785543060, api_factory=telegram, process_checker=lambda _pid: True)
        self.write_deployment(
            downloaded_bytes=110,
            percentage=11,
            last_successful_byte_growth_time="2026-08-01T00:11:01Z",
        )
        notifier.deliver(now=1785543061, api_factory=telegram, process_checker=lambda _pid: True)
        notifier.deliver(now=1785543062, api_factory=telegram, process_checker=lambda _pid: True)
        alerts = [call[2] for call in telegram.calls if call[0] == "send"]
        self.assertEqual(sum("download RECOVERED" in text for text in alerts), 1)

    def test_retry_state_is_immediate_and_not_failed(self) -> None:
        telegram = MockTelegram()
        self.write_deployment(
            current_phase="DOWNLOADING_GDOWN",
            downloader_process_state="RETRYING",
            retries=2,
        )
        notifier.deliver(now=1785542410, api_factory=telegram, process_checker=lambda _pid: False)
        alerts = [call[2] for call in telegram.calls if call[0] == "send"]
        self.assertTrue(any("download RETRYING" in text for text in alerts))
        self.assertFalse(any("download FAILED" in text for text in alerts))

    def test_download_display_is_telemetry_bound_and_bot_pending(self) -> None:
        telegram = MockTelegram()
        self.write_deployment(
            current_phase="DOWNLOADING_RCLONE",
            downloader_name="rclone",
            current_file="segment-07.duckdb.partial",
            current_file_bytes=123,
            current_file_expected_bytes=1000,
            downloaded_bytes=456,
            total_bytes=220754442143,
            current_rate=0,
            eta_seconds=None,
            last_successful_byte_growth_time="2026-08-01T00:01:00Z",
        )
        notifier.deliver(now=1785542470, api_factory=telegram, process_checker=lambda _pid: True)
        progress = [call[2] for call in telegram.calls if call[0] == "send"][0]
        self.assertIn("Current file: segment-07.duckdb.partial", progress)
        self.assertIn("Total bytes: 456 / 220754442143", progress)
        self.assertIn("Current 60-second speed: 0 B/s", progress)
        self.assertIn("ETA: UNKNOWN", progress)
        self.assertIn("Main bot: PENDING_DOWNLOAD", progress)
        self.assertNotIn("Main bot: unhealthy", progress)

    def test_deployment_entrypoint_never_fails_for_telegram(self) -> None:
        with mock.patch.object(notifier, "deliver", return_value=False):
            self.assertEqual(notifier.main(["update"]), 0)

    def test_no_polling_or_command_line_secrets(self) -> None:
        source = (REPOSITORY / "deploy" / "compactdb-telegram-notifier.py").read_text(encoding="utf-8")
        unit = (REPOSITORY / "deploy" / "compactdb-notifier.service").read_text(encoding="utf-8")
        self.assertNotIn("getUpdates", source)
        self.assertNotIn("BOT_TOKEN", unit)
        self.assertNotIn("BOT_OWNER_IDS", unit)
        self.assertIn("/usr/bin/python3 /usr/local/libexec/compactdb-telegram-notifier.py update", unit)


if __name__ == "__main__":
    unittest.main()
