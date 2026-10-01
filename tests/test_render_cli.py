import json
import math
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
PROJECT_SCRIPT = ROOT / "plan_to_mlt.py"
RENDER_SCRIPT = ROOT / "render_mlt.py"


class RenderCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.mkv"
        self.plan = self.root / "edit.plan.json"
        self.project = self.root / "edit.mlt"
        self.output = self.root / "edit.mp4"

    def make_media(self):
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for color, frequency in (("red", 440), ("green", 660), ("blue", 880)):
            command.extend(["-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=10:d=1"])
            command.extend(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration=1"])
        command.extend([
            "-filter_complex", "[0:v][1:a][2:v][3:a][4:v][5:a]concat=n=3:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(self.source),
        ])
        subprocess.run(command, check=True, capture_output=True)

    def make_multiaudio_media(self):
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=1",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(self.source),
        ], check=True, capture_output=True)

    def make_project(self):
        self.make_media()
        self.generate_project([
            {"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"},
            {"source_start": 1, "source_end": 2, "keep": False},
            {"source_start": 2, "source_end": 3, "keep": True, "join_after": "cut"},
        ])
        self.assertFalse(self.output.exists())

    def generate_project(self, segments, audio_stream=None):
        plan = {
            "version": 1,
            "source": self.source.name,
            "tail_policy": "discard",
            "segments": segments,
            "annotations": [],
        }
        if audio_stream is not None:
            plan["audio_stream"] = audio_stream
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        generated = subprocess.run([sys.executable, str(PROJECT_SCRIPT), str(self.plan)], capture_output=True, text=True)
        self.assertEqual(generated.returncode, 0, generated.stderr)

    def render(self, *args):
        return subprocess.run([sys.executable, str(RENDER_SCRIPT), str(self.project), *args], capture_output=True, text=True)

    def test_render_keeps_selected_video_and_audio_with_shareable_codecs(self):
        self.make_project()
        result = self.render("--crf", "20", "--preset", "fast", "--audio-bitrate", "128k")
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(self.output),
        ], capture_output=True, text=True, check=True)
        media = json.loads(probe.stdout)
        self.assertEqual([(s["codec_type"], s["codec_name"]) for s in media["streams"]],
                         [("video", "h264"), ("audio", "aac")])
        self.assertEqual(media["streams"][0]["pix_fmt"], "yuv420p")
        self.assertAlmostEqual(float(media["format"]["duration"]), 2.0, delta=0.15)

        frames = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], capture_output=True, check=True).stdout
        frame_size = 64 * 64 * 3
        self.assertEqual(len(frames) // frame_size, 20)
        centers = [frames[n * frame_size + (32 * 64 + 32) * 3:n * frame_size + (32 * 64 + 32) * 3 + 3]
                   for n in range(20)]
        self.assertTrue(all(pixel[0] > 160 and pixel[1] < 80 for pixel in centers[:10]), centers)
        self.assertTrue(all(pixel[2] > 160 and pixel[1] < 80 for pixel in centers[10:]), centers)

        audio = subprocess.run([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-vn", "-ac", "1", "-ar", "44100",
            "-f", "s16le", "-",
        ], capture_output=True, check=True).stdout
        samples = struct.unpack(f"<{len(audio) // 2}h", audio)
        for tenth in range(20):
            offset = tenth / 10
            expected = 440 if tenth < 10 else 880
            window = samples[int(offset * 44100):int((offset + 0.1) * 44100)]
            def strength(frequency):
                return abs(sum(sample * math.sin(2 * math.pi * frequency * i / 44100)
                               for i, sample in enumerate(window)))
            self.assertGreater(strength(expected), strength(660) * 5)
            self.assertGreater(strength(expected), 100_000)

    def test_existing_output_requires_replace_and_failed_render_preserves_it(self):
        self.make_project()
        self.output.write_bytes(b"existing video")
        refused = self.render()
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--replace", refused.stderr)
        self.assertEqual(self.output.read_bytes(), b"existing video")
        replaced = self.render("--replace")
        self.assertEqual(replaced.returncode, 0, replaced.stderr)
        self.assertNotEqual(self.output.read_bytes(), b"existing video")
        original = self.output.read_bytes()
        failed = self.render("--replace", "--preset", "invalid-preset")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.output.read_bytes(), original)

    def test_project_and_encoder_failures_are_reported_without_output(self):
        missing = self.render()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("project", missing.stderr)
        self.project.write_text("not XML", encoding="utf-8")
        invalid = self.render()
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("MLT project", invalid.stderr)
        self.project.unlink()
        self.make_project()
        self.source.unlink()
        missing_source = self.render()
        self.assertNotEqual(missing_source.returncode, 0)
        self.assertIn("source", missing_source.stderr)
        self.assertFalse(self.output.exists())
        self.make_media()
        failed_encoder = self.render("--preset", "invalid-preset")
        self.assertNotEqual(failed_encoder.returncode, 0)
        self.assertIn("preset", failed_encoder.stderr.lower())
        self.assertFalse(self.output.exists())

    def test_video_only_project_has_no_audio_stream(self):
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:r=10:d=1",
            "-c:v", "ffv1", str(self.source),
        ], check=True, capture_output=True)
        self.generate_project([{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}])
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(self.output),
        ], capture_output=True, text=True, check=True)
        self.assertEqual([stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]], ["video"])

    def test_selected_recording_audio_is_heard_in_project_and_mp4(self):
        self.make_multiaudio_media()
        self.generate_project(
            [{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}],
            audio_stream=2,
        )
        preview = self.root / "preview.mkv"
        generated_preview = subprocess.run([
            "melt", str(self.project), "-consumer", f"avformat:{preview}",
            "vcodec=ffv1", "acodec=pcm_s16le", "real_time=-1",
        ], capture_output=True, text=True)
        self.assertEqual(generated_preview.returncode, 0, generated_preview.stderr)
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)

        for output in (preview, self.output):
            with self.subTest(output=output.name):
                probe = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(output),
                ], capture_output=True, text=True, check=True)
                self.assertEqual([stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]],
                                 ["video", "audio"])
                audio = subprocess.run([
                    "ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1", "-ar", "44100",
                    "-f", "s16le", "-",
                ], capture_output=True, check=True).stdout
                samples = struct.unpack(f"<{len(audio) // 2}h", audio)
                window = samples[11025:33075]
                def strength(frequency):
                    return abs(sum(sample * math.sin(2 * math.pi * frequency * i / 44100)
                                   for i, sample in enumerate(window)))
                self.assertGreater(strength(880), 100_000)
                self.assertGreater(strength(880), strength(440) * 10)

    def test_render_rejects_project_with_implicit_audio_selection(self):
        self.make_multiaudio_media()
        self.generate_project(
            [{"source_start": 0, "source_end": 1, "keep": True, "join_after": "cut"}],
            audio_stream=2,
        )
        tree = ET.parse(self.project)
        producer = tree.find("./producer")
        assert producer is not None
        for item in producer.findall("property"):
            if item.get("name") == "audio_index":
                producer.remove(item)
        tree.write(self.project, encoding="utf-8", xml_declaration=True)

        rendered = self.render()
        self.assertNotEqual(rendered.returncode, 0)
        self.assertIn("audio_index", rendered.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
