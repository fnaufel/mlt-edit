#!/usr/bin/env python3
"""Generate an MLT project from an independently edited JSON plan."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
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


def load_plan(path: Path) -> tuple[Path, list[Segment], Path | None, int | None]:
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
        duration = None
        if join == "dissolve":
            duration = seconds(raw.get("transition_duration"), f"{label} transition_duration")
            if duration <= 0:
                raise PlanError(f"{label} transition_duration must be positive")
        segments.append(Segment(start, end, keep, join, duration))
        previous_end = end

    if not any(segment.keep for segment in segments):
        raise PlanError("plan has no kept segments; there is no project to generate")
    transitions = [index for index, segment in enumerate(segments) if segment.join_after == "dissolve"]
    if len(transitions) > 1:
        raise PlanError("multiple transitions and transition chains are not supported yet")
    if transitions:
        index = transitions[0]
        if index == len(segments) - 1:
            raise PlanError(f"segment {index + 1} has a terminal transition without a following kept segment")
        if not segments[index + 1].keep:
            raise PlanError(f"segment {index + 1} transition across discarded footage is not supported yet")
        duration = segments[index].transition_duration
        assert duration is not None
        if duration > segments[index].end - segments[index].start or duration > segments[index + 1].end - segments[index + 1].start:
            raise PlanError(f"segment {index + 1} transition_duration needs more footage than an adjacent kept segment provides")

    audio_index = data.get("audio_stream")
    if audio_index is not None and (type(audio_index) is not int or audio_index < 0):
        raise PlanError("audio_stream must be a nonnegative stream index")
    audio_file = data.get("audio_file")
    if audio_file is not None and (not isinstance(audio_file, str) or not audio_file.strip()):
        raise PlanError("audio_file must name one audio file")
    source_path = Path(source)
    if not source_path.is_absolute():
        source_path = path.parent / source_path
    audio_path = None
    if audio_file is not None:
        audio_path = Path(audio_file)
        if not audio_path.is_absolute():
            audio_path = path.parent / audio_path
        audio_path = audio_path.resolve()
    return source_path.resolve(), segments, audio_path, audio_index


def probe_recording(path: Path, selected_audio: int | None, external_audio: bool = False) -> Recording:
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


def project_xml(segments: list[Segment], recording: Recording, external_audio: ExternalAudio | None = None) -> bytes:
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

    transition = next((index for index, (_, segment, _, _) in enumerate(kept) if segment.join_after == "dissolve"), None)
    if transition is None:
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
        _, outgoing, left_start, left_end = kept[transition]
        _, _, right_start, right_end = kept[transition + 1]
        duration = outgoing.transition_duration
        assert duration is not None
        overlap = frame_at(duration, recording.frame_rate)
        left_frames = left_end - left_start
        right_frames = right_end - right_start
        if overlap < 1 or overlap > min(left_frames, right_frames):
            raise PlanError("transition_duration needs at least one frame and cannot exceed either adjacent segment")
        left_length = sum(end - start for _, _, start, end in kept[:transition + 1])
        overlap_start = left_length - overlap
        output_frames = sum(end - start for _, _, start, end in kept) - overlap

        video_lanes = [ET.SubElement(mlt, "playlist", {"id": f"video_{side}"}) for side in ("before", "after")]
        audio_lanes = ([ET.SubElement(mlt, "playlist", {"id": f"audio_{side}"}) for side in ("before", "after")]
                       if external_audio is not None else [])
        if overlap_start:
            ET.SubElement(video_lanes[1], "blank", {"length": str(overlap_start)})
        if audio_lanes and overlap_start:
            ET.SubElement(audio_lanes[1], "blank", {"length": str(overlap_start)})
        for index, (_, _, start, end) in enumerate(kept):
            side = 0 if index <= transition else 1
            entry = {"in": str(start), "out": str(end - 1)}
            ET.SubElement(video_lanes[side], "entry", {"producer": "source", **entry})
            if audio_lanes:
                ET.SubElement(audio_lanes[side], "entry", {"producer": "external_audio", **entry})

    tractor = ET.SubElement(mlt, "tractor", {"id": "project", "in": "0", "out": str(output_frames - 1)})
    if transition is None:
        ET.SubElement(tractor, "track", {"producer": "kept"})
        if external_audio is not None:
            ET.SubElement(tractor, "track", {"producer": "kept_audio"})
    else:
        for lane in video_lanes + audio_lanes:
            ET.SubElement(tractor, "track", {"producer": lane.attrib["id"]})
        audio_tracks = (2, 3) if external_audio is not None else (0, 1)
        for service, a_track, b_track in (("luma", 0, 1), ("mix", *audio_tracks)):
            if service == "mix" and recording.audio_index < 0 and external_audio is None:
                continue
            effect = ET.SubElement(tractor, "transition", {
                "in": str(overlap_start), "out": str(left_length - 1),
            })
            for property_name, property_value in (("a_track", a_track), ("b_track", b_track), ("mlt_service", service)):
                ET.SubElement(effect, "property", {"name": property_name}).text = str(property_value)
            if service == "mix":
                ET.SubElement(effect, "property", {"name": "start"}).text = "-1"
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
        source, segments, audio_file, selected_audio = load_plan(args.plan)
        recording = probe_recording(source, selected_audio, audio_file is not None)
        external_audio = probe_external_audio(audio_file, selected_audio) if audio_file else None
        xml = project_xml(segments, recording, external_audio)
        with project.open("wb" if args.replace else "xb") as file:
            file.write(xml)
    except FileExistsError:
        parser.error(f"project already exists: {project}; use --replace to overwrite it")
    except PlanError as error:
        parser.error(str(error))
    print(f"project: {project}")
    if recording.audio_index < 0 and external_audio is None and any(segment.join_after == "dissolve" for segment in segments):
        print("warning: no audio source selected; no audio crossfade is possible", file=sys.stderr)
    print(f"source: {source}")
    print(f"frame rate: {recording.frame_rate}, resolution: {recording.width}x{recording.height}")


if __name__ == "__main__":
    main()
