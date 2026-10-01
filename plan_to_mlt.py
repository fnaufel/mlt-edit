#!/usr/bin/env python3
"""Generate a cut-only MLT project from an independently edited JSON plan."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


class PlanError(ValueError):
    """A plan or recording cannot be rendered safely."""


@dataclass(frozen=True)
class Segment:
    start: Decimal
    end: Decimal
    keep: bool
    join_after: str | None


@dataclass(frozen=True)
class Recording:
    path: Path
    frame_rate: Fraction
    width: int
    height: int
    video_index: int
    frame_count: int
    duration: Decimal
    audio_index: int
    sample_aspect: Fraction
    progressive: bool


def seconds(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{label} must be a finite number of seconds")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise PlanError(f"{label} must be a finite number of seconds") from error
    if not result.is_finite():
        raise PlanError(f"{label} must be a finite number of seconds")
    return result


def load_plan(path: Path) -> tuple[Path, list[Segment], int | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PlanError(f"cannot read plan {path}: {error}") from error
    if not isinstance(data, dict):
        raise PlanError("plan must be a JSON object")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise PlanError("unsupported plan version; expected version 1")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PlanError("plan source must name one recording file")
    if data.get("tail_policy") != "discard":
        raise PlanError("tail_policy must be 'discard'")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise PlanError("plan must contain source-order segments")

    segments: list[Segment] = []
    previous_end = Decimal(0)
    for number, raw in enumerate(raw_segments, 1):
        label = f"segment {number}"
        if not isinstance(raw, dict):
            raise PlanError(f"{label} must be an object")
        start = seconds(raw.get("source_start"), f"{label} source_start")
        end = seconds(raw.get("source_end"), f"{label} source_end")
        if start != previous_end:
            raise PlanError(f"{label} must start at {previous_end} seconds to keep source-order contiguous intervals")
        if end <= start:
            raise PlanError(f"{label} must have positive duration (source_end > source_start)")
        keep = raw.get("keep")
        if type(keep) is not bool:
            raise PlanError(f"{label} keep must be true or false")
        join = raw.get("join_after")
        if join not in (None, "cut", "dissolve"):
            raise PlanError(f"{label} join_after must be 'cut', 'dissolve', or null")
        if not keep and join is not None:
            raise PlanError(f"{label} is discarded and cannot have a join_after")
        segments.append(Segment(start, end, keep, join))
        previous_end = end

    if not any(segment.keep for segment in segments):
        raise PlanError("plan has no kept segments; there is no project to generate")
    if any(segment.join_after == "dissolve" for segment in segments):
        raise PlanError("transition join 'dissolve' is not supported by cut-only project generation")

    audio_index = data.get("audio_stream")
    if audio_index is not None and (type(audio_index) is not int or audio_index < 0):
        raise PlanError("audio_stream must be a nonnegative stream index")
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    return source_path.resolve(), segments, audio_index


def probe_recording(path: Path, selected_audio: int | None) -> Recording:
    if not path.is_file():
        raise PlanError(f"source recording does not exist: {path}; correct plan source")
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "stream=index,codec_type,width,height,avg_frame_rate,sample_aspect_ratio,field_order:format=duration",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        raise PlanError(f"cannot run ffprobe: {error}") from error
    if result.returncode:
        raise PlanError(f"cannot inspect source recording {path}: {result.stderr.strip()}")
    details = json.loads(result.stdout)
    streams = details.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise PlanError(f"source recording must contain one video stream; found {len(videos)}")
    video = videos[0]
    try:
        rate = Fraction(video["avg_frame_rate"])
        width = int(video["width"])
        height = int(video["height"])
        duration = Decimal(details["format"]["duration"])
        aspect_text = video.get("sample_aspect_ratio", "1:1")
        sample_aspect = (Fraction(1, 1) if aspect_text in ("N/A", "0:1", "0:0")
                         else Fraction(aspect_text.replace(":", "/")))
    except (KeyError, ValueError, ZeroDivisionError) as error:
        raise PlanError(f"source recording has invalid frame rate, resolution, or duration: {error}") from error
    if rate <= 0 or width <= 0 or height <= 0 or not duration.is_finite() or duration <= 0 or sample_aspect <= 0:
        raise PlanError("source recording has invalid frame rate, resolution, or duration")

    if selected_audio is None:
        if len(audios) > 1:
            raise PlanError("source recording has multiple audio streams; set plan audio_stream to a stream index")
        audio_index = int(audios[0]["index"]) if audios else -1
    else:
        if selected_audio not in [stream["index"] for stream in audios]:
            raise PlanError(f"audio_stream {selected_audio} is not an audio stream in the source recording")
        audio_index = selected_audio

    timestamps = probe_frame_times(path, int(video["index"]))
    if not timestamps:
        raise PlanError("source recording has no video frames")
    expected_step = Decimal(rate.denominator) / Decimal(rate.numerator)
    tolerance = expected_step / 20
    for number, (left, right) in enumerate(zip(timestamps, timestamps[1:]), 2):
        if abs((right - left) - expected_step) > tolerance:
            raise PlanError(f"variable-frame-rate source recording near video frame {number}; constant frame rate required")
    return Recording(path, rate, width, height, int(video["index"]), len(timestamps), duration,
                     audio_index, sample_aspect, video.get("field_order") not in {"tt", "bb", "tb", "bt"})


def probe_frame_times(path: Path, video_index: int) -> list[Decimal]:
    command = [
        "ffprobe", "-v", "error", "-select_streams", str(video_index),
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", str(path),
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except OSError as error:
        raise PlanError(f"cannot run ffprobe: {error}") from error
    if result.returncode:
        raise PlanError(f"cannot inspect video frame times: {result.stderr.strip()}")
    try:
        return [Decimal(line.split(",", 1)[0]) for line in result.stdout.splitlines() if line.strip()]
    except InvalidOperation as error:
        raise PlanError("source recording has unreadable video frame times") from error


def frame_at(value: Decimal, rate: Fraction) -> int:
    frames = value * Decimal(rate.numerator) / Decimal(rate.denominator)
    return int(frames.to_integral_value(rounding=ROUND_HALF_UP))


def project_xml(segments: list[Segment], recording: Recording) -> bytes:
    video_duration = Decimal(recording.frame_count * recording.frame_rate.denominator) / Decimal(recording.frame_rate.numerator)
    for number, segment in enumerate(segments, 1):
        if segment.end > video_duration:
            raise PlanError(f"segment {number} ends beyond source recording duration ({video_duration} seconds)")
    mlt = ET.Element("mlt", {"LC_NUMERIC": "C"})
    display_aspect = Fraction(recording.width, recording.height) * recording.sample_aspect
    ET.SubElement(mlt, "profile", {
        "description": "Source recording",
        "width": str(recording.width), "height": str(recording.height),
        "progressive": "1" if recording.progressive else "0",
        "sample_aspect_num": str(recording.sample_aspect.numerator),
        "sample_aspect_den": str(recording.sample_aspect.denominator),
        "display_aspect_num": str(display_aspect.numerator),
        "display_aspect_den": str(display_aspect.denominator),
        "frame_rate_num": str(recording.frame_rate.numerator),
        "frame_rate_den": str(recording.frame_rate.denominator),
    })
    producer = ET.SubElement(mlt, "producer", {"id": "source"})
    for name, value in (
        ("mlt_service", "avformat"), ("resource", str(recording.path)),
        ("video_index", str(recording.video_index)), ("audio_index", str(recording.audio_index)),
    ):
        ET.SubElement(producer, "property", {"name": name}).text = value
    playlist = ET.SubElement(mlt, "playlist", {"id": "kept"})
    output_frames = 0
    for number, segment in enumerate(segments, 1):
        if not segment.keep:
            continue
        start = frame_at(segment.start, recording.frame_rate)
        end = frame_at(segment.end, recording.frame_rate)
        if end <= start:
            raise PlanError(f"segment {number} has no video frames after frame quantization")
        if end > recording.frame_count:
            raise PlanError(f"segment {number} ends beyond source recording duration ({recording.duration} seconds)")
        ET.SubElement(playlist, "entry", {"producer": "source", "in": str(start), "out": str(end - 1)})
        output_frames += end - start
    tractor = ET.SubElement(mlt, "tractor", {"id": "project", "in": "0", "out": str(output_frames - 1)})
    ET.SubElement(tractor, "track", {"producer": "kept"})
    ET.indent(mlt)
    return ET.tostring(mlt, encoding="utf-8", xml_declaration=True) + b"\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Saved JSON edit plan")
    parser.add_argument("--project", type=Path, help="Output MLT project (default: plan stem + .mlt)")
    parser.add_argument("--replace", action="store_true", help="Replace an existing MLT project")
    args = parser.parse_args()
    project = args.project or args.plan.with_suffix("").with_suffix(".mlt")
    try:
        source, segments, selected_audio = load_plan(args.plan)
        recording = probe_recording(source, selected_audio)
        xml = project_xml(segments, recording)
        with project.open("wb" if args.replace else "xb") as file:
            file.write(xml)
    except FileExistsError:
        parser.error(f"project already exists: {project}; use --replace to overwrite it")
    except PlanError as error:
        parser.error(str(error))
    print(f"project: {project}")
    print(f"source: {source}")
    print(f"frame rate: {recording.frame_rate}, resolution: {recording.width}x{recording.height}")


if __name__ == "__main__":
    main()
