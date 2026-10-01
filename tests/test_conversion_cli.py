import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "obs_csv_to_mlt.py"
CONFIG = ROOT / "edit-config.toml"


class ConversionCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.csv_path = self.root / "markers.csv"
        self.source = self.root / "recording.mkv"

    def write_csv(self, markers):
        with self.csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(
                ["Recording Timestamp on File", "Recording End Mark Timestamp on File", "Recording Full Path", "Comment"]
            )
            for timestamp, marker in markers:
                writer.writerow([timestamp, "", str(self.source), marker])

    def convert(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.csv_path), "-c", str(CONFIG), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def test_conversion_writes_only_versioned_contiguous_plan_with_resolved_transition(self):
        self.write_csv(
            [
                ("00:00:02", "KEEP_TRANSITION"),
                ("00:00:04", "DELETE"),
                ("00:00:06", "KEEP_CUT"),
            ]
        )

        result = self.convert()

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads((self.root / "markers.plan.json").read_text())
        self.assertEqual(plan["version"], 1)
        self.assertEqual(plan["source"], "recording.mkv")
        self.assertEqual(
            [(segment["source_start"], segment["source_end"], segment["keep"]) for segment in plan["segments"]],
            [(0, 2, True), (2, 4, False), (4, 6, True)],
        )
        self.assertEqual(plan["segments"][0]["join_after"], "dissolve")
        self.assertEqual(plan["segments"][0]["transition_duration"], 0.5)
        self.assertFalse((self.root / "markers.mlt").exists())
        self.assertFalse((self.root / "markers.mp4").exists())

    def test_conversion_saves_all_discarded_plan_without_media(self):
        self.write_csv([("00:00:03", "DELETE"), ("00:00:07", "DELETE")])

        result = self.convert()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.source.exists())
        plan = json.loads((self.root / "markers.plan.json").read_text())
        self.assertEqual(
            [(segment["source_start"], segment["source_end"], segment["keep"]) for segment in plan["segments"]],
            [(0, 3, False), (3, 7, False)],
        )
        self.assertEqual(plan["tail_policy"], "discard")

    def test_source_reference_is_relative_to_custom_plan_location(self):
        self.write_csv([("00:00:03", "KEEP_CUT")])
        plan_path = self.root / "plans" / "editable.json"
        plan_path.parent.mkdir()

        result = self.convert("--plan", str(plan_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(plan_path.read_text())
        self.assertEqual(plan["source"], "../recording.mkv")

    def test_configured_transition_duration_is_saved_in_plan(self):
        self.write_csv([("00:00:03", "KEEP_TRANSITION"), ("00:00:06", "KEEP_CUT")])
        config_path = self.root / "custom.toml"
        config_path.write_text(
            CONFIG.read_text().replace('tail = "discard"', 'tail = "discard"\ntransition_duration = 1.25')
        )

        result = self.convert("-c", str(config_path))

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads((self.root / "markers.plan.json").read_text())
        self.assertEqual(plan["segments"][0]["transition_duration"], 1.25)

    def test_existing_hand_edits_require_explicit_replacement(self):
        self.write_csv([("00:00:03", "KEEP_CUT")])
        plan_path = self.root / "markers.plan.json"
        plan_path.write_text('{"hand_edited": true}\n')

        refused = self.convert()

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--replace", refused.stderr)
        self.assertEqual(plan_path.read_text(), '{"hand_edited": true}\n')

        replaced = self.convert("--replace")

        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertEqual(json.loads(plan_path.read_text())["version"], 1)


if __name__ == "__main__":
    unittest.main()
