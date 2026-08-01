#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "compactdb_progress", REPOSITORY / "deploy" / "compactdb-progress.py"
)
assert SPEC and SPEC.loader
progress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(progress)


class ProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.manifest = self.root / "manifest.json"
        self.history = self.root / "history.json"
        self.state = self.root / "deployment.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "file_count": 2,
                    "total_bytes": 1000,
                    "files": [
                        {"path": "large.bin", "size": 900},
                        {"path": "small.json", "size": 100},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.state.write_text('{"current_phase":"DOWNLOADING_GDOWN"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def resize(self, name: str, size: int) -> Path:
        path = self.package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab"):
            pass
        os.truncate(path, size)
        return path

    def snapshot(
        self,
        epoch: float,
        monotonic: float,
        downloader: str = "gdown",
        pid: int = 101,
        process_state: str = "RUNNING",
        boot: str = "boot-a",
    ) -> dict[str, object]:
        return progress.update_progress(
            self.package,
            self.manifest,
            self.history,
            self.state,
            downloader,
            pid,
            process_state,
            epoch,
            monotonic,
            boot,
        )

    def test_authoritative_29_file_manifest(self) -> None:
        raw = [
            {
                "Path": f"file-{index:02d}.bin",
                "Size": 1 if index else progress.EXPECTED_BYTES - 28,
                "IsDir": False,
            }
            for index in range(progress.EXPECTED_FILES)
        ]
        manifest = progress.normalize_rclone_manifest(raw)
        self.assertEqual(manifest["file_count"], 29)
        self.assertEqual(manifest["total_bytes"], 220754442143)

    def test_gdown_partial_grows_each_cycle(self) -> None:
        partial = self.resize("large.binrandom.part", 100)
        values = [self.snapshot(1000, 10)["downloaded_bytes"]]
        for index, size in enumerate((200, 300, 450), start=1):
            os.truncate(partial, size)
            values.append(self.snapshot(1000 + index * 15, 10 + index * 15)["downloaded_bytes"])
        self.assertEqual(values, [100, 200, 300, 450])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["current_file"], "large.binrandom.part")
        self.assertEqual(state["current_file_bytes"], 450)
        self.assertEqual(state["current_file_expected_bytes"], 900)

    def test_partial_rename_does_not_double_count(self) -> None:
        partial = self.resize("large.binrandom.part", 500)
        before = self.snapshot(1000, 10)
        partial.replace(self.package / "large.bin")
        after = self.snapshot(1015, 25)
        self.assertEqual(before["downloaded_bytes"], 500)
        self.assertEqual(after["downloaded_bytes"], 500)
        self.assertEqual(after["completed_files"], 0)
        os.truncate(self.package / "large.bin", 900)
        complete = self.snapshot(1030, 40)
        self.assertEqual(complete["downloaded_bytes"], 900)
        self.assertEqual(complete["completed_files"], 1)

    def test_restart_and_reboot_never_regress(self) -> None:
        partial = self.resize("large.binrandom.part", 500)
        first = self.snapshot(1000, 10, pid=101)
        os.truncate(partial, 200)
        restarted = self.snapshot(1030, 40, pid=202)
        rebooted = self.snapshot(1060, 5, pid=303, boot="boot-b")
        self.assertEqual(first["downloaded_bytes"], 500)
        self.assertEqual(restarted["downloaded_bytes"], 500)
        self.assertEqual(rebooted["downloaded_bytes"], 500)
        self.assertEqual(rebooted["current_rate"], 0)

    def test_gdown_to_rclone_switch_is_monotonic(self) -> None:
        partial = self.resize("large.binrandom.part", 400)
        gdown = self.snapshot(1000, 10, downloader="gdown", pid=101)
        partial.unlink()
        self.resize("large.bin", 250)
        rclone_start = self.snapshot(1030, 40, downloader="rclone", pid=202)
        os.truncate(self.package / "large.bin", 600)
        rclone_growth = self.snapshot(1060, 70, downloader="rclone", pid=202)
        self.assertEqual(gdown["downloaded_bytes"], 400)
        self.assertEqual(rclone_start["downloaded_bytes"], 400)
        self.assertEqual(rclone_growth["downloaded_bytes"], 600)

    def test_rclone_partial_grows_and_clamps(self) -> None:
        partial = self.resize(".large.bin.transfer.partial", 100)
        values = [self.snapshot(1000, 10, downloader="rclone")["downloaded_bytes"]]
        for index, size in enumerate((250, 400, 1200), start=1):
            os.truncate(partial, size)
            values.append(self.snapshot(1000 + 15 * index, 10 + 15 * index, downloader="rclone")["downloaded_bytes"])
        self.assertEqual(values, [100, 250, 400, 900])
        self.assertEqual(self.snapshot(1060, 70, downloader="rclone")["completed_files"], 0)
        self.assertTrue(partial.exists())

    def test_rolling_speed_zero_speed_and_eta(self) -> None:
        partial = self.resize("large.binrandom.part", 0)
        self.snapshot(1000, 10)
        os.truncate(partial, 300)
        middle = self.snapshot(1030, 40)
        os.truncate(partial, 600)
        moving = self.snapshot(1060, 70)
        stalled = self.snapshot(1075, 85)
        self.assertEqual(middle["current_rate"], 10)
        self.assertEqual(moving["current_rate"], 10)
        self.assertGreater(moving["average_rate"], 0)
        self.assertIsNotNone(moving["eta_seconds"])
        self.assertEqual(stalled["current_rate"], 0)
        self.assertIsNone(stalled["eta_seconds"])

    def test_percentage_advances_before_file_completion(self) -> None:
        partial = self.resize("large.binrandom.part", 100)
        percentages = [self.snapshot(1000, 10)["percentage"]]
        for index, size in enumerate((200, 300, 400, 500), start=1):
            os.truncate(partial, size)
            percentages.append(self.snapshot(1000 + 15 * index, 10 + 15 * index)["percentage"])
        self.assertEqual(percentages, [10.0, 20.0, 30.0, 40.0, 50.0])


if __name__ == "__main__":
    unittest.main()
