import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import yaml
import yaml_to_mlt


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "yaml_to_mlt.py"


class ProjectCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "corrected source.mkv"
        self.plan_path = self.root / "edit.plan.yaml"
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

    def make_media_with_frame_gap(self):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=3",
            "-vf", r"select=not(eq(n\,15))", "-fps_mode", "vfr", "-c:v", "ffv1",
            str(self.source),
        ], check=True, capture_output=True)

    def save_plan(self, segments, **changes):
        plan = {
            "version": 1,
            "source": self.source.name,
            "tail_policy": "discard",
            "segments": segments,
            "annotations": [],
        }
        plan.update(changes)
        self.plan_path.write_text(yaml.safe_dump(plan), encoding="utf-8")

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

    def test_timecode_frame_suffix_sets_rendered_boundaries(self):
        self.make_media()
        self.save_plan([
            {"source_start": "00:00:00:00", "source_end": "00:00:00:02", "keep": False},
            {"source_start": "00:00:00:02", "source_end": "00:00:00:08", "keep": True, "join_after": "cut"},
        ])
        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertEqual(
            [(entry.attrib["in"], entry.attrib["out"]) for entry in ET.parse(self.project).findall("./playlist/entry")],
            [("2", "7")],
        )
        self.save_plan([
            {"source_start": "00:00:00:00", "source_end": "00:00:00:10", "keep": True, "join_after": "cut"},
        ])
        invalid = self.generate()
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("frame number", invalid.stderr)

    def test_audio_file_path_can_be_corrected_after_move(self):
        self.make_media()
        audio = self.root / "old.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            str(audio),
        ], check=True, capture_output=True)
        moved = self.root / "media" / "new.wav"
        moved.parent.mkdir()
        audio.rename(moved)
        self.save_plan([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
        ], audio_file="media/new.wav")

        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        producers = ET.parse(self.project).findall("./producer")
        resources = [{item.get("name"): item.text for item in producer.findall("property")}
                     for producer in producers]
        self.assertEqual(len(resources), 2)
        self.assertEqual(resources[0]["audio_index"], "-1")
        self.assertEqual(resources[1]["resource"], str(moved))
        self.assertEqual(resources[1]["video_index"], "-1")

    def test_external_audio_errors_leave_no_project(self):
        self.make_media()
        segments = [
            {"source_start": 0, "source_end": 2, "keep": True, "join_after": "cut"},
        ]
        self.save_plan(segments, audio_file="missing.wav")
        missing = self.generate()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("audio_file does not exist", missing.stderr)
        self.assertFalse(self.project.exists())

        silent = self.root / "silent.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=64x64:r=10:d=2",
            "-c:v", "ffv1", str(silent),
        ], check=True, capture_output=True)
        self.save_plan(segments, audio_file=silent.name)
        no_audio = self.generate()
        self.assertNotEqual(no_audio.returncode, 0)
        self.assertIn("no audio streams", no_audio.stderr)
        self.assertFalse(self.project.exists())

        short = self.root / "short.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            str(short),
        ], check=True, capture_output=True)
        self.save_plan(segments, audio_file=short.name)
        too_short = self.generate()
        self.assertNotEqual(too_short.returncode, 0)
        self.assertIn("beyond audio_file duration", too_short.stderr)
        self.assertFalse(self.project.exists())

        multiple = self.root / "multiple.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=1400:duration=2",
            "-map", "0:a", "-map", "1:a", "-c:a", "pcm_s16le", str(multiple),
        ], check=True, capture_output=True)
        self.save_plan(segments, audio_file=multiple.name)
        ambiguous = self.generate()
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertIn("multiple audio streams", ambiguous.stderr)
        self.assertFalse(self.project.exists())

        unequal = self.root / "unequal.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=1400:duration=3",
            "-map", "0:a", "-map", "1:a", "-c:a", "pcm_s16le", str(unequal),
        ], check=True, capture_output=True)
        self.save_plan(segments, audio_file=unequal.name, audio_stream=0)
        selected_short = self.generate()
        self.assertNotEqual(selected_short.returncode, 0)
        self.assertIn("beyond audio_file duration", selected_short.stderr)
        self.assertFalse(self.project.exists())

    def test_invalid_hand_edits_report_the_fault_without_writing_a_project(self):
        kept = {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}
        cases: list[tuple[dict[str, object], list[dict[str, object]], str]] = [
            (dict(version=2), [kept], "version"),
            (dict(source=[self.source.name]), [kept], "source"),
            ({}, [{**kept, "source_start": 0.1}], "segment 1"),
            ({}, [kept, {**kept, "source_start": 1.1, "source_end": 2}], "segment 2"),
            ({}, [{**kept, "source_end": 0}], "source_end"),
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

    def test_yaml_syntax_and_schema_errors_leave_no_project(self):
        for content, message in [
            ("segments: [\n", "cannot read plan"),
            ("version: 1\nsource: recording.mkv\ntail_policy: discard\nsegments:\n  - source_start: 0\n    source_end: 1\n    keep: maybe\nannotations: []\n", "keep"),
        ]:
            with self.subTest(message=message):
                self.plan_path.write_text(content)
                result = self.generate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.project.exists())

    def test_impossible_transitions_leave_no_project(self):
        self.make_media()
        first = {"source_start": 0, "source_end": 1, "keep": True, "join_after": "dissolve", "transition_duration": 0.5}
        second = {"source_start": 1, "source_end": 2, "keep": True, "join_after": "cut"}
        cases = [
            ([first], "terminal transition"),
            ([{**first, "transition_duration": 1.1}, second], "more footage"),
            ([first, {**second, "source_end": 1.3}], "more footage"),
            ([first, {"source_start": 1, "source_end": 2, "keep": False}], "following kept segment"),
            ([{**first, "transition_duration": 0.6},
              {**second, "join_after": "dissolve", "transition_duration": 0.5},
              {"source_start": 2, "source_end": 3, "keep": True, "join_after": "cut"}], "combined overlap"),
            ([{**first, "transition_duration": 0.35},
              {"source_start": 1, "source_end": 1.6, "keep": True, "join_after": "dissolve", "transition_duration": 0.3},
              {"source_start": 1.6, "source_end": 3, "keep": True, "join_after": "cut"}], "combined overlap"),
            ([{**first, "transition_duration": 0.25},
              {"source_start": 1, "source_end": 1.6, "keep": True, "join_after": "dissolve", "transition_duration": 0.35},
              {"source_start": 1.6, "source_end": 3, "keep": True, "join_after": "cut"}], "after frame quantization"),
            ([{**first, "transition_duration": 0}, second], "transition_duration"),
            ([{**first, "transition_duration": 0.01}, second], "at least one frame"),
            ([{key: value for key, value in first.items() if key != "transition_duration"}, second], "transition_duration"),
        ]
        for segments, message in cases:
            with self.subTest(message=message):
                self.save_plan(segments)
                result = self.generate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.project.exists())

    def test_transition_crosses_multiple_discarded_segments_in_project(self):
        self.make_media()
        self.save_plan([
            {"source_start": 0, "source_end": 0.8, "keep": True, "join_after": "dissolve", "transition_duration": 0.3},
            {"source_start": 0.8, "source_end": 1.4, "keep": False},
            {"source_start": 1.4, "source_end": 2, "keep": False},
            {"source_start": 2, "source_end": 3, "keep": True, "join_after": "cut"},
        ])
        generated = self.generate()
        self.assertEqual(generated.returncode, 0, generated.stderr)
        xml = ET.parse(self.project)
        tractor = xml.find("./tractor")
        assert tractor is not None
        self.assertEqual(tractor.get("out"), "14")
        self.assertEqual(
            [(entry.get("in"), entry.get("out")) for entry in xml.findall("./playlist/entry")],
            [("0", "7"), ("20", "29")],
        )

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

    def test_discarded_interval_beyond_recording_after_last_kept_is_ignored(self):
        self.make_media()
        self.save_plan([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
            {"source_start": 1, "source_end": 4, "keep": False},
        ])
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.project.exists())
        self.assertEqual(len(ET.parse(self.project).findall(".//playlist[@id='kept']/entry")), 1)

    def test_kept_interval_beyond_recording_is_rejected(self):
        self.make_media()
        self.save_plan([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
            {"source_start": 1, "source_end": 4, "keep": True, "join_after": "cut"},
        ])
        errors = io.StringIO()
        with patch.object(yaml_to_mlt, "probe_frame_times", side_effect=AssertionError("frame scan started")):
            with patch.object(sys, "argv", [str(SCRIPT), str(self.plan_path)]):
                with redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as stopped:
                        yaml_to_mlt.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("segment 2 ends at 4 seconds", errors.getvalue())
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

    def test_frame_gap_after_final_kept_segment_is_ignored(self):
        self.make_media_with_frame_gap()
        kept = {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}
        for segments in (
            [kept],
            [kept, {"source_start": 1, "source_end": 3, "keep": False}],
        ):
            with self.subTest(segments=segments):
                self.save_plan(segments)
                self.project.unlink(missing_ok=True)
                result = self.generate()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.project.exists())

    def test_frame_gap_before_later_kept_segment_is_rejected(self):
        self.make_media_with_frame_gap()
        self.save_plan([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
            {"source_start": 1, "source_end": 2, "keep": False},
            {"source_start": 2, "source_end": 3, "keep": True, "join_after": "cut"},
        ])
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("variable-frame-rate", result.stderr)
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
        self.assertFalse(self.project.exists())
        for invalid in (0, 3, -1, "2", True):
            with self.subTest(audio_stream=invalid):
                self.save_plan(segments, audio_stream=invalid)
                rejected = self.generate()
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("audio_stream", rejected.stderr)
                self.assertFalse(self.project.exists())
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
