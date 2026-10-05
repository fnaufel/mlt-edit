import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from yaml_to_mlt import load_plan


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "migrate_plan.py"


class MigrateCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.old = self.root / "edit.plan.yaml"
        self.new = self.root / "edit.plan.v2.yaml"
        self.plan: dict[str, Any] = {
            "version": 1,
            "source": "recording.mkv",
            "tail_policy": "discard",
            "audio_file": "replacement.wav",
            "segments": [
                {"source_start": "00:00:00", "source_end": "00:00:01", "keep": True,
                 "join_after": "dissolve", "transition_duration": 0.2, "marker": "old label"},
                {"source_start": "00:00:01", "source_end": "00:00:02", "keep": False, "marker": "old label"},
                {"source_start": "00:00:02", "source_end": 3.5, "keep": True,
                 "join_after": "cut", "marker": "old label"},
            ],
            "annotations": [{"time": "00:00:01", "marker": "TITLE", "kind": "point", "csv_row": 2}],
        }
        self.old.write_text(yaml.safe_dump(self.plan), encoding="utf-8")

    def run_migration(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.old), *args],
            cwd=self.root, text=True, capture_output=True,
        )

    def test_migration_preserves_render_decisions_and_original(self):
        old_text = self.old.read_text()
        result = self.run_migration()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.old.read_text(), old_text)
        migrated = yaml.safe_load(self.new.read_text())
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["segments"], [
            {"source_end": "00:00:01", "marker": "KEEP_TRANSITION", "transition_duration": 0.2},
            {"source_end": "00:00:02", "marker": "DELETE"},
            {"source_end": 3.5, "marker": "KEEP_CUT"},
        ])
        self.assertEqual(migrated["annotations"], self.plan["annotations"])
        schema = json.loads((ROOT / "edit-plan.schema.json").read_text())
        jsonschema.validate(migrated, schema)
        _, segments, audio, _ = load_plan(self.new)
        self.assertEqual([(segment.start, segment.end, segment.keep, segment.join_after) for segment in segments], [
            (0, 1, True, "dissolve"), (1, 2, False, None), (2, 3.5, True, "cut"),
        ])
        self.assertEqual(audio, self.root / "replacement.wav")

    def test_custom_output_rebases_media_paths(self):
        output = self.root / "plans" / "converted.yaml"
        output.parent.mkdir()
        result = self.run_migration("--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = yaml.safe_load(output.read_text())
        self.assertEqual(plan["source"], "../recording.mkv")
        self.assertEqual(plan["audio_file"], "../replacement.wav")
        self.assertEqual((output.parent / plan["source"]).resolve(), self.root / "recording.mkv")

    def test_existing_output_is_reported_before_input_processing(self):
        self.new.write_text("hand edits\n")
        self.old.unlink()
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plan already exists", result.stderr)
        self.assertNotIn("No such file", result.stderr)
        self.assertEqual(self.new.read_text(), "hand edits\n")

    def test_replace_and_same_path_protection(self):
        self.new.write_text("old output\n")
        self.assertEqual(self.run_migration("--replace").returncode, 0)
        self.assertEqual(yaml.safe_load(self.new.read_text())["version"], 2)
        refused = self.run_migration("--output", str(self.old), "--replace")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("must differ", refused.stderr)
        self.assertEqual(yaml.safe_load(self.old.read_text())["version"], 1)

    def test_invalid_old_boundaries_are_rejected(self):
        self.plan["segments"][1]["source_start"] = "00:00:01:01"
        self.old.write_text(yaml.safe_dump(self.plan))
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source recording does not exist", result.stderr)
        self.assertFalse(self.new.exists())

        self.plan["segments"][1]["source_start"] = "00:00:00"
        self.old.write_text(yaml.safe_dump(self.plan))
        result = self.run_migration()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("segment 2 must start", result.stderr)
        self.assertFalse(self.new.exists())


if __name__ == "__main__":
    unittest.main()
