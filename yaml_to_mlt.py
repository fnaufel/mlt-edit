#!/usr/bin/env python3
"""Generate an MLT project from an independently edited YAML plan."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import jsonschema
import yaml
from tqdm import tqdm

from plan_format import MARKER_DECISIONS


SCHEMA_PATH = Path(__file__).with_name("edit-plan.schema.json")
TIMECODE = re.compile(r"([0-9]{2,}):([0-5][0-9]):([0-5][0-9])(?::([0-9]{2,}))?")


class PlanError(ValueError):
    """A plan or recording cannot be rendered safely."""


@dataclass(frozen=True)
class Segment:
    start: Decimal
    end: Decimal
    keep: bool
    join_after: str | None
    transition_duration: Decimal | None = None


@dataclass(frozen=True)
class Recording:
    path: Path
    frame_rate: Fraction
    width: int
    height: int
    video_index: int
    frame_count: int
    duration: Decimal
    video_duration: Decimal
    audio_index: int
    sample_aspect: Fraction
    progressive: bool


@dataclass(frozen=True)
class ExternalAudio:
    path: Path
    stream_index: int
    duration: Decimal


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


def probe_frame_rate(path: Path) -> Fraction:
    if not path.is_file():
        raise PlanError(f"source recording does not exist: {path}; correct plan source")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True, capture_output=True, check=False,
        )
    except OSError as error:
        raise PlanError(f"cannot run ffprobe: {error}") from error
    if result.returncode:
        raise PlanError(f"cannot inspect source recording {path}: {result.stderr.strip()}")
    try:
        rate = Fraction(result.stdout.strip())
    except (ValueError, ZeroDivisionError) as error:
        raise PlanError(f"source recording has invalid frame rate: {result.stdout.strip()!r}") from error
    if rate <= 0:
        raise PlanError("source recording has invalid frame rate")
    return rate


def position_seconds(value: Any, label: str, frame_rate: Fraction | None) -> Decimal:
    if not isinstance(value, str):
        return seconds(value, label)
    match = TIMECODE.fullmatch(value)
    if match is None:
        raise PlanError(f"{label} must be HH:MM:SS or HH:MM:SS:FF")
    hours, minutes, whole_seconds = (int(part) for part in match.group(1, 2, 3))
    result = Decimal(hours * 3600 + minutes * 60 + whole_seconds)
    frames = match.group(4)
    if frames is not None:
        assert frame_rate is not None
        frame = int(frames)
        if frame >= math.ceil(frame_rate):
            raise PlanError(f"{label} frame number must be below {math.ceil(frame_rate)} at {frame_rate} fps")
        result += Decimal(frame * frame_rate.denominator) / Decimal(frame_rate.numerator)
    return result


def load_plan(path: Path) -> tuple[Path, list[Segment], Path | None, int | None]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PlanError(f"cannot read plan {path}: {error}") from error
    if isinstance(data, dict) and data.get("version") == 1:
        raise PlanError("version 1 plan; convert it with migrate_plan.py")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "plan"
        raise PlanError(f"{location}: {error.message}") from error
    if not isinstance(data, dict):
        raise PlanError("plan must be a mapping")
    if type(data.get("version")) is not int or data["version"] != 2:
        raise PlanError("unsupported plan version; expected version 2")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PlanError("plan source must name one recording file")
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    if data.get("tail_policy") != "discard":
        raise PlanError("tail_policy must be 'discard'")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise PlanError("plan must contain source-order segments")
    has_frames = any(
        isinstance(raw, dict) and isinstance(raw.get("source_end"), str) and raw["source_end"].count(":") == 3
        for raw in raw_segments
    )
    frame_rate = probe_frame_rate(source_path) if has_frames else None

    segments: list[Segment] = []
    previous_end = Decimal(0)
    for number, raw in enumerate(raw_segments, 1):
        label = f"segment {number}"
        if not isinstance(raw, dict):
            raise PlanError(f"{label} must be an object")
        start = previous_end
        end = position_seconds(raw.get("source_end"), f"{label} source_end", frame_rate)
        if end <= start:
            raise PlanError(f"{label} source_end must be later than the previous boundary ({start} seconds)")
        keep, join = MARKER_DECISIONS[raw["marker"]]
        duration = None
        if join == "dissolve":
            duration = seconds(raw.get("transition_duration"), f"{label} transition_duration")
            if duration <= 0:
                raise PlanError(f"{label} transition_duration must be positive")
        segments.append(Segment(start, end, keep, join, duration))
        previous_end = end

    if not any(segment.keep for segment in segments):
        raise PlanError("plan has no kept segments; there is no project to generate")
    kept = [(number, segment) for number, segment in enumerate(segments, 1) if segment.keep]
    for index, (number, segment) in enumerate(kept):
        if segment.join_after != "dissolve":
            continue
        if index == len(kept) - 1:
            detail = "terminal transition" if number == len(segments) else "transition"
            raise PlanError(f"segment {number} has a {detail} without a following kept segment")
        duration = segment.transition_duration
        assert duration is not None
        next_segment = kept[index + 1][1]
        if duration > segment.end - segment.start or duration > next_segment.end - next_segment.start:
            raise PlanError(f"segment {number} transition_duration needs more footage than an adjacent kept segment provides")
        if index > 0:
            previous = kept[index - 1][1]
            incoming = previous.transition_duration if previous.join_after == "dissolve" else None
            if incoming is not None and incoming + duration > segment.end - segment.start:
                raise PlanError(f"segment {number} combined overlap exceeds the shared kept segment")

    audio_index = data.get("audio_stream")
    if audio_index is not None and (type(audio_index) is not int or audio_index < 0):
        raise PlanError("audio_stream must be a nonnegative stream index")
    audio_file = data.get("audio_file")
    if audio_file is not None and (not isinstance(audio_file, str) or not audio_file.strip()):
        raise PlanError("audio_file must name one audio file")
    audio_path = None
    if audio_file is not None:
        audio_path = Path(audio_file)
        if not audio_path.is_absolute():
            audio_path = path.parent / audio_path
        audio_path = audio_path.resolve()
    return source_path.resolve(), segments, audio_path, audio_index


def probe_recording(path: Path, selected_audio: int | None, required_through: Decimal, required_segment: int,
                    external_audio: bool = False) -> Recording:
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
    if required_through > duration:
        raise PlanError(
            f"segment {required_segment} ends at {required_through} seconds, "
            f"beyond source recording duration ({duration} seconds)"
        )

    if external_audio:
        audio_index = -1
    elif selected_audio is None:
        if len(audios) > 1:
            raise PlanError("source recording has multiple audio streams; set plan audio_stream to a stream index")
        audio_index = int(audios[0]["index"]) if audios else -1
    else:
        if selected_audio not in [stream["index"] for stream in audios]:
            raise PlanError(f"audio_stream {selected_audio} is not an audio stream in the source recording")
        audio_index = selected_audio

    timestamps = probe_frame_times(path, int(video["index"]), duration)
    if not timestamps:
        raise PlanError("source recording has no video frames")
    expected_step = Decimal(rate.denominator) / Decimal(rate.numerator)
    tolerance = expected_step / 20
    for number, (left, right) in enumerate(zip(timestamps, timestamps[1:]), 2):
        if left >= required_through:
            break
        if abs((right - left) - expected_step) > tolerance:
            raise PlanError(f"variable-frame-rate source recording near video frame {number}; constant frame rate required")
    # A frame gap after the edit can reduce frame_count without shortening the source timeline.
    video_duration = min(duration, timestamps[-1] + expected_step)
    return Recording(path, rate, width, height, int(video["index"]), len(timestamps), duration, video_duration,
                     audio_index, sample_aspect, video.get("field_order") not in {"tt", "bb", "tb", "bt"})


def probe_external_audio(path: Path, selected_stream: int | None) -> ExternalAudio:
    if not path.is_file():
        raise PlanError(f"audio_file does not exist: {path}; correct plan audio_file")
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "stream=index,codec_type,duration:stream_tags=DURATION:format=duration",
            "-of", "json", str(path),
        ], text=True, capture_output=True, check=False)
    except OSError as error:
        raise PlanError(f"cannot run ffprobe: {error}") from error
    if result.returncode:
        raise PlanError(f"cannot inspect audio_file {path}: {result.stderr.strip()}")
    details = json.loads(result.stdout)
    audios = [stream for stream in details.get("streams", []) if stream.get("codec_type") == "audio"]
    if not audios:
        raise PlanError(f"audio_file has no audio streams: {path}")
    if selected_stream is None:
        if len(audios) > 1:
            raise PlanError("audio_file has multiple audio streams; set plan audio_stream to a stream index")
        audio_index = int(audios[0]["index"])
    else:
        if selected_stream not in [stream["index"] for stream in audios]:
            raise PlanError(f"audio_stream {selected_stream} is not an audio stream in audio_file")
        audio_index = selected_stream
    selected = next(stream for stream in audios if stream["index"] == audio_index)
    duration_text = selected.get("duration") or selected.get("tags", {}).get("DURATION")
    if duration_text is None:
        duration_text = details.get("format", {}).get("duration")
    try:
        if ":" in duration_text:
            hours, minutes, seconds_part = duration_text.split(":")
            duration = Decimal(hours) * 3600 + Decimal(minutes) * 60 + Decimal(seconds_part)
        else:
            duration = Decimal(duration_text)
    except (TypeError, AttributeError, ValueError, InvalidOperation) as error:
        raise PlanError(f"audio_file has no usable duration: {path}") from error
    if not duration.is_finite() or duration <= 0:
        raise PlanError(f"audio_file has no usable duration: {path}")
    return ExternalAudio(path, audio_index, duration)


def probe_frame_times(path: Path, video_index: int, duration: Decimal) -> list[Decimal]:
    command = [
        "ffprobe", "-v", "error", "-select_streams", str(video_index),
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", str(path),
    ]
    timestamps: list[Decimal] = []
    invalid_timestamp = False
    try:
        with tempfile.TemporaryFile(mode="w+t") as errors:
            with subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=errors) as process:
                assert process.stdout is not None
                total_seconds = float(duration)
                scanned_seconds = 0.0
                with tqdm(total=total_seconds, desc="Scanning video frames", unit="s",
                          file=sys.stderr, disable=not sys.stderr.isatty()) as progress:
                    for line in process.stdout:
                        if not line.strip():
                            continue
                        try:
                            timestamp = Decimal(line.split(",", 1)[0])
                        except InvalidOperation:
                            invalid_timestamp = True
                            continue
                        if not timestamp.is_finite():
                            invalid_timestamp = True
                            continue
                        timestamps.append(timestamp)
                        next_seconds = float(min(max(timestamp, Decimal(0)), duration))
                        if next_seconds > scanned_seconds:
                            progress.update(next_seconds - scanned_seconds)
                            scanned_seconds = next_seconds
                    returncode = process.wait()
                    if returncode == 0 and timestamps and not invalid_timestamp:
                        progress.update(total_seconds - scanned_seconds)
            if returncode:
                errors.seek(0)
                raise PlanError(f"cannot inspect video frame times: {errors.read().strip()}")
    except OSError as error:
        raise PlanError(f"cannot run ffprobe: {error}") from error
    if invalid_timestamp:
        raise PlanError("source recording has unreadable video frame times")
    return timestamps


def frame_at(value: Decimal, rate: Fraction) -> int:
    frames = value * Decimal(rate.numerator) / Decimal(rate.denominator)
    return int(frames.to_integral_value(rounding=ROUND_HALF_UP))


def project_xml(segments: list[Segment], recording: Recording, external_audio: ExternalAudio | None = None) -> bytes:
    for number, segment in enumerate(segments, 1):
        if segment.keep and segment.end > recording.video_duration:
            raise PlanError(f"segment {number} ends beyond source recording duration ({recording.video_duration} seconds)")
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
    if external_audio is not None:
        audio_producer = ET.SubElement(mlt, "producer", {"id": "external_audio"})
        for name, value in (
            ("mlt_service", "avformat"), ("resource", str(external_audio.path)),
            ("video_index", "-1"), ("audio_index", str(external_audio.stream_index)),
        ):
            ET.SubElement(audio_producer, "property", {"name": name}).text = value
    kept: list[tuple[int, Segment, int, int]] = []
    for number, segment in enumerate(segments, 1):
        if not segment.keep:
            continue
        start = frame_at(segment.start, recording.frame_rate)
        end = frame_at(segment.end, recording.frame_rate)
        if end <= start:
            raise PlanError(f"segment {number} has no video frames after frame quantization")
        if end > recording.frame_count:
            raise PlanError(f"segment {number} ends beyond source recording duration ({recording.duration} seconds)")
        if external_audio is not None and segment.end > external_audio.duration:
            raise PlanError(f"kept segment {number} ends beyond audio_file duration ({external_audio.duration} seconds)")
        kept.append((number, segment, start, end))

    overlaps = [0] * (len(kept) - 1)
    for index, (number, segment, start, end) in enumerate(kept[:-1]):
        if segment.join_after != "dissolve":
            continue
        duration = segment.transition_duration
        assert duration is not None
        overlap = frame_at(duration, recording.frame_rate)
        next_start, next_end = kept[index + 1][2:]
        if overlap < 1 or overlap > min(end - start, next_end - next_start):
            raise PlanError(f"segment {number} transition_duration needs at least one frame and cannot exceed either adjacent segment")
        overlaps[index] = overlap
    for index, (number, _, start, end) in enumerate(kept[1:-1], 1):
        if overlaps[index - 1] + overlaps[index] > end - start:
            raise PlanError(f"segment {number} combined overlap exceeds the shared kept segment after frame quantization")

    if not any(overlaps):
        playlist = ET.SubElement(mlt, "playlist", {"id": "kept"})
        audio_playlist = ET.SubElement(mlt, "playlist", {"id": "kept_audio"}) if external_audio else None
        for _, _, start, end in kept:
            ET.SubElement(playlist, "entry", {"producer": "source", "in": str(start), "out": str(end - 1)})
            if audio_playlist is not None:
                ET.SubElement(audio_playlist, "entry", {
                    "producer": "external_audio", "in": str(start), "out": str(end - 1),
                })
        output_frames = sum(end - start for _, _, start, end in kept)
    else:
        lane_count = 1 + sum(overlap > 0 for overlap in overlaps)
        video_lanes = [ET.SubElement(mlt, "playlist", {"id": f"video_{index}"}) for index in range(lane_count)]
        audio_lanes = ([ET.SubElement(mlt, "playlist", {"id": f"audio_{index}"}) for index in range(lane_count)]
                       if external_audio is not None else [])
        starts = [0]
        lanes = [0]
        lane_ends = [0] * lane_count
        for index, (_, _, start, end) in enumerate(kept):
            if index:
                previous_length = kept[index - 1][3] - kept[index - 1][2]
                starts.append(starts[-1] + previous_length - overlaps[index - 1])
                lanes.append(lanes[-1] + (overlaps[index - 1] > 0))
            lane = lanes[index]
            entry = {"in": str(start), "out": str(end - 1)}
            if starts[index] > lane_ends[lane]:
                ET.SubElement(video_lanes[lane], "blank", {"length": str(starts[index] - lane_ends[lane])})
                if audio_lanes:
                    ET.SubElement(audio_lanes[lane], "blank", {"length": str(starts[index] - lane_ends[lane])})
            ET.SubElement(video_lanes[lane], "entry", {"producer": "source", **entry})
            if audio_lanes:
                ET.SubElement(audio_lanes[lane], "entry", {"producer": "external_audio", **entry})
            lane_ends[lane] = starts[index] + end - start
        output_frames = starts[-1] + kept[-1][3] - kept[-1][2]

    tractor = ET.SubElement(mlt, "tractor", {"id": "project", "in": "0", "out": str(output_frames - 1)})
    if not any(overlaps):
        ET.SubElement(tractor, "track", {"producer": "kept"})
        if external_audio is not None:
            ET.SubElement(tractor, "track", {"producer": "kept_audio"})
    else:
        for playlist in video_lanes + audio_lanes:
            ET.SubElement(tractor, "track", {"producer": playlist.attrib["id"]})
        for index, overlap in enumerate(overlaps):
            if not overlap:
                continue
            for service in ("luma", "mix"):
                if service == "mix" and recording.audio_index < 0 and external_audio is None:
                    continue
                offset = lane_count if service == "mix" and external_audio is not None else 0
                effect = ET.SubElement(tractor, "transition", {
                    "in": str(starts[index + 1]), "out": str(starts[index + 1] + overlap - 1),
                })
                for property_name, property_value in (
                    ("a_track", lanes[index] + offset), ("b_track", lanes[index + 1] + offset),
                    ("mlt_service", service),
                ):
                    ET.SubElement(effect, "property", {"name": property_name}).text = str(property_value)
                if service == "mix":
                    ET.SubElement(effect, "property", {"name": "start"}).text = "-1"
    ET.indent(mlt)
    return ET.tostring(mlt, encoding="utf-8", xml_declaration=True) + b"\n"


def preview_xml(project: bytes, recording: Recording, segments: list[Segment]) -> bytes:
    mlt = ET.fromstring(project)
    source = mlt.find("./producer[@id='source']")
    tractor = mlt.find("./tractor[@id='project']")
    assert source is not None and tractor is not None

    def add_filter(parent: ET.Element, service: str, properties: dict[str, str]) -> None:
        effect = ET.SubElement(parent, "filter", {"mlt_service": service})
        for name, value in properties.items():
            ET.SubElement(effect, "property", {"name": name}).text = value

    def duration(frames: int) -> str:
        total_seconds = math.ceil(Fraction(frames, 1) / recording.frame_rate) + 1
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.000"

    source_size = str(max(18, round(recording.height / 30)))
    edit_size = str(max(14, round(recording.height / 40)))
    common = {"fgcolour": "white", "bgcolour": "0x00000080", "pad": "6", "halign": "left", "valign": "top"}
    add_filter(source, "timer", {
        **common, "format": "HH:MM:SS.S", "direction": "up",
        "duration": duration(recording.frame_count), "size": source_size,
        "geometry": "2%/7%:55%x8%",
    })
    segment_numbers = {
        frame_at(segment.start, recording.frame_rate): number
        for number, segment in enumerate(segments, 1) if segment.keep
    }
    for entry in mlt.findall("./playlist/entry[@producer='source']"):
        number = segment_numbers[int(entry.attrib["in"])]
        add_filter(entry, "dynamictext", {
            **common, "argument": f"SEGMENT {number}", "size": edit_size,
            "geometry": "2%/15%:55%x8%",
        })
    add_filter(tractor, "dynamictext", {
        **common, "argument": "SOURCE", "size": source_size,
        "geometry": "2%/2%:55%x8%",
    })
    add_filter(tractor, "dynamictext", {
        **common, "argument": "EDIT", "size": edit_size,
        "geometry": "62%/2%:36%x8%",
    })
    add_filter(tractor, "timer", {
        **common, "format": "HH:MM:SS.S", "direction": "up",
        "duration": duration(int(tractor.attrib["out"]) + 1), "size": edit_size,
        "geometry": "62%/7%:36%x8%",
    })
    ET.indent(mlt)
    return ET.tostring(mlt, encoding="utf-8", xml_declaration=True) + b"\n"


def check_project_paths(paths: list[Path], replace: bool) -> None:
    if paths[0].resolve() == paths[1].resolve():
        raise PlanError("production and preview project paths must differ")
    if not replace:
        for path in paths:
            if path.exists() or path.is_symlink():
                raise PlanError(f"project already exists: {path}; use --replace to overwrite it")


def write_projects(outputs: list[tuple[Path, bytes]], replace: bool) -> None:
    check_project_paths([path for path, _ in outputs], replace)
    staged: list[Path] = []
    created: list[Path] = []
    try:
        for path, data in outputs:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.stem}-", suffix=".mlt", delete=False) as file:
                staged.append(Path(file.name))
                file.write(data)
        for (path, _), temporary in zip(outputs, staged):
            if replace:
                temporary.replace(path)
            else:
                os.link(temporary, path)
                created.append(path)
    except FileExistsError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise PlanError(f"project already exists: {error.filename2 or error.filename}; use --replace to overwrite it") from error
    except OSError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise PlanError(f"cannot write MLT projects: {error}") from error
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Saved YAML edit plan")
    parser.add_argument("--project", type=Path, help="Output MLT project (default: plan stem + .mlt)")
    parser.add_argument("--preview-project", type=Path, help="Output preview MLT project (default: project stem + -preview.mlt)")
    parser.add_argument("--replace", action="store_true", help="Replace existing production and preview MLT projects")
    args = parser.parse_args()
    project = args.project or args.plan.with_suffix("").with_suffix(".mlt")
    preview_project = args.preview_project or project.with_name(f"{project.stem}-preview.mlt")
    try:
        check_project_paths([project, preview_project], args.replace)
        print("Loading edit plan...", file=sys.stderr, flush=True)
        source, segments, audio_file, selected_audio = load_plan(args.plan)
        last_kept_number = max(number for number, segment in enumerate(segments, 1) if segment.keep)
        last_kept_end = segments[last_kept_number - 1].end
        print("Inspecting recording...", file=sys.stderr, flush=True)
        recording = probe_recording(source, selected_audio, last_kept_end, last_kept_number, audio_file is not None)
        if audio_file:
            print("Inspecting external audio...", file=sys.stderr, flush=True)
        external_audio = probe_external_audio(audio_file, selected_audio) if audio_file else None
        print("Building MLT project...", file=sys.stderr, flush=True)
        xml = project_xml(segments, recording, external_audio)
        preview = preview_xml(xml, recording, segments)
        print("Writing MLT projects...", file=sys.stderr, flush=True)
        write_projects([(project, xml), (preview_project, preview)], args.replace)
    except PlanError as error:
        parser.error(str(error))
    print(f"project: {project}")
    print(f"preview: {preview_project}")
    if recording.audio_index < 0 and external_audio is None and any(segment.join_after == "dissolve" for segment in segments):
        print("warning: no audio source selected; no audio crossfade is possible", file=sys.stderr)
    print(f"source: {source}")
    print(f"frame rate: {recording.frame_rate}, resolution: {recording.width}x{recording.height}")


if __name__ == "__main__":
    main()
