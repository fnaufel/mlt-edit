import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plan_to_mlt.py"


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "corrected source.mkv"
        self.plan_path = self.root / "edit.plan.json"
        self.project = self.root / "edit.mlt"

    def make_media(self):
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for color in ("red", "green", "blue"):
            command.extend(["-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=10:d=1"])
        command.extend([
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map", "[v]", "-map", "3:a", "-c:v", "ffv1", "-c:a", "pcm_s16le",
            "-shortest", str(self.source),
        ])
        subprocess.run(command, check=True, capture_output=True)

    def save_plan(self, segments, **changes):
        plan = {
            "version": 1,
            "source": self.source.name,
            "tail_policy": "discard",
            "segments": segments,
            "annotations": [],
        }
        plan.update(changes)
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def generate(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.plan_path), *args],
            cwd=self.root, text=True, capture_output=True,
        )

    def test_corrected_fractional_plan_renders_only_kept_footage(self):
        self.make_media()
        self.save_plan([
            {"source_start": 0, "source_end": 0.2, "keep": False},
            {"source_start": 0.2, "source_end": 0.8, "keep": True, "join_after": "cut"},
            {"source_start": 0.8, "source_end": 2.2, "keep": False},
            {"source_start": 2.2, "source_end": 2.8, "keep": True, "join_after": "cut"},
        ])

        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.project.exists())
        xml = ET.parse(self.project)
        profile = xml.find("profile")
        assert profile is not None
        self.assertEqual(profile.attrib["frame_rate_num"], "10")
        self.assertEqual(profile.attrib["width"], "64")
        self.assertEqual(profile.attrib["height"], "64")
        self.assertEqual(
            [(entry.attrib["in"], entry.attrib["out"]) for entry in xml.findall("./playlist/entry")],
            [("2", "7"), ("22", "27")],
        )
        rendered = self.root / "rendered.mkv"
        melt = subprocess.run([
            "melt", str(self.project), "-consumer", f"avformat:{rendered}",
            "vcodec=ffv1", "acodec=pcm_s16le", "real_time=-1",
        ], text=True, capture_output=True)
        self.assertEqual(melt.returncode, 0, melt.stderr)
        frames = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(rendered),
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], capture_output=True, check=True).stdout
        size = 64 * 64 * 3
        self.assertEqual(len(frames) // size, 12)
        centers = [frames[index * size + (32 * 64 + 32) * 3:index * size + (32 * 64 + 32) * 3 + 3]
                   for index in (0, 5, 6, 11)]
        self.assertTrue(all(color[0] > 200 and color[1] < 50 for color in centers[:2]), centers)
        self.assertTrue(all(color[2] > 200 and color[1] < 50 for color in centers[2:]), centers)

    def test_invalid_hand_edits_report_the_fault_without_writing_a_project(self):
        kept = {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}
        cases: list[tuple[dict[str, object], list[dict[str, object]], str]] = [
            (dict(version=2), [kept], "version"),
            (dict(source=[self.source.name]), [kept], "source"),
            ({}, [{**kept, "source_start": 0.1}], "segment 1"),
            ({}, [kept, {**kept, "source_start": 1.1, "source_end": 2}], "segment 2"),
            ({}, [{**kept, "source_end": 0}], "positive duration"),
            ({}, [{**kept, "join_after": "wipe"}], "join_after"),
            ({}, [{**kept, "keep": False, "join_after": None}], "no kept segments"),
            ({}, [{**kept, "join_after": "dissolve"}], "transition"),
        ]
        for changes, segments, message in cases:
            with self.subTest(message=message):
                self.save_plan(segments, **changes)
                result = self.generate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.project.exists())

    def test_project_requires_explicit_replacement(self):
        self.make_media()
        self.save_plan([{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}])
        self.assertEqual(self.generate().returncode, 0)
        original = self.project.read_bytes()
        self.save_plan([{"source_start": 0, "source_end": 2, "keep": True, "join_after": "cut"}])
        refused = self.generate()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--replace", refused.stderr)
        self.assertEqual(self.project.read_bytes(), original)
        self.assertEqual(self.generate("--replace").returncode, 0)
        self.assertNotEqual(self.project.read_bytes(), original)

    def test_discarded_interval_beyond_recording_is_rejected(self):
        self.make_media()
        self.save_plan([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
            {"source_start": 1, "source_end": 4, "keep": False},
        ])
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("segment 2", result.stderr)
        self.assertIn("beyond source recording", result.stderr)
        self.assertFalse(self.project.exists())

    def test_missing_source_and_variable_frame_rate_report_clear_errors(self):
        self.save_plan([{"source_start": 0, "source_end": 0.5, "keep": True, "join_after": "cut"}])
        missing = self.generate()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("correct plan source", missing.stderr)

        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=1",
            "-vf", r"select=not(eq(n\,5))", "-fps_mode", "vfr", "-c:v", "ffv1",
            str(self.source),
        ], check=True, capture_output=True)
        variable = self.generate()
        self.assertNotEqual(variable.returncode, 0)
        self.assertIn("variable-frame-rate", variable.stderr)
        self.assertFalse(self.project.exists())

    def test_multiple_audio_streams_require_valid_explicit_selection(self):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=s=64x64:r=10:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(self.source),
        ], check=True, capture_output=True)
        segments = [{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}]
        self.save_plan(segments)
        ambiguous = self.generate()
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("multiple audio streams", ambiguous.stderr)
        self.save_plan(segments, audio_stream=2)
        selected = self.generate()
        self.assertEqual(selected.returncode, 0, selected.stderr)
        properties = {item.attrib["name"]: item.text for item in ET.parse(self.project).findall("./producer/property")}
        self.assertEqual(properties["audio_index"], "2")

    def test_video_stream_can_follow_audio_stream(self):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=1",
            "-map", "0:a", "-map", "1:v", "-c:a", "pcm_s16le", "-c:v", "ffv1",
            str(self.source),
        ], check=True, capture_output=True)
        self.save_plan([{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}])

        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        properties = {item.attrib["name"]: item.text for item in ET.parse(self.project).findall("./producer/property")}
        self.assertEqual(properties["video_index"], "1")
        self.assertEqual(properties["audio_index"], "0")

    def test_video_only_source_generates_project_without_audio(self):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=1",
            "-c:v", "ffv1", str(self.source),
        ], check=True, capture_output=True)
        self.save_plan([{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}])

        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        properties = {item.attrib["name"]: item.text for item in ET.parse(self.project).findall("./producer/property")}
        self.assertEqual(properties["audio_index"], "-1")
        self.assertEqual(subprocess.run([
            "melt", str(self.project), "-consumer", f"avformat:{self.root / 'silent.mkv'}",
            "vcodec=ffv1", "real_time=-1",
        ], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
