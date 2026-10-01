#!/usr/bin/env python3
"""Render an existing MLT project to a protected MP4 with melt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET


class RenderError(ValueError):
    """The project or encoder cannot produce a usable MP4."""


def check_project(project: Path) -> bool:
    try:
        root = ET.parse(project).getroot()
    except FileNotFoundError as error:
        raise RenderError(f"project does not exist: {project}") from error
    except (OSError, ET.ParseError) as error:
        raise RenderError(f"cannot read MLT project {project}: {error}") from error
    if root.tag != "mlt":
        raise RenderError(f"project is not MLT XML: {project}")
    audio_indices: list[str | None] = []
    for producer in root.findall(".//producer"):
        properties = {item.get("name"): item.text for item in producer.findall("property")}
        if properties.get("mlt_service") != "avformat":
            continue
        audio_index = properties.get("audio_index")
        if audio_index is None:
            raise RenderError("project source has no audio_index; regenerate the project with an explicit audio selection")
        audio_indices.append(audio_index)
        resource = properties.get("resource")
        if not resource:
            raise RenderError(f"project has an avformat producer without a source: {project}")
        if "://" not in resource:
            source = Path(resource)
            if not source.is_absolute():
                source = project.parent / source
            if not source.is_file():
                raise RenderError(f"project source does not exist: {source}")
    return any(index != "-1" for index in audio_indices) if audio_indices else True


def render(project: Path, output: Path, replace: bool, crf: int, preset: str, audio_bitrate: str) -> None:
    if project.resolve() == output.resolve():
        raise RenderError("MP4 output must differ from the MLT project")
    if output.exists() and not replace:
        raise RenderError(f"MP4 already exists: {output}; use --replace to overwrite it")
    has_audio = check_project(project)

    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".render-", suffix=".mp4", dir=output.parent)
    except OSError as error:
        raise RenderError(f"cannot create MP4 beside {output}: {error}") from error
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        command = [
            "melt", str(project), "-consumer", f"avformat:{temporary}",
            "vcodec=libx264", "pix_fmt=yuv420p", "acodec=aac", f"crf={crf}", f"preset={preset}",
            f"ab={audio_bitrate}", "movflags=+faststart", "real_time=-1",
        ]
        if not has_audio:
            command.append("an=1")
        encoded = subprocess.run(command, capture_output=True, text=True)
        if encoded.returncode:
            raise RenderError(f"melt failed: {encoded.stderr.strip() or encoded.stdout.strip() or f'exit status {encoded.returncode}'}")
        probed = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(temporary),
        ], capture_output=True, text=True)
        if probed.returncode:
            raise RenderError(f"encoded MP4 is invalid: {probed.stderr.strip() or 'ffprobe failed'}; melt: {encoded.stderr.strip()}")
        media = json.loads(probed.stdout)
        streams = media.get("streams", [])
        if not any(stream.get("codec_type") == "video" and stream.get("codec_name") == "h264" for stream in streams):
            raise RenderError(f"encoded MP4 has no H.264 video; melt: {encoded.stderr.strip()}")
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if has_audio and not any(stream.get("codec_name") == "aac" for stream in audio_streams):
            raise RenderError(f"encoded MP4 has no AAC audio; melt: {encoded.stderr.strip()}")
        if not has_audio and audio_streams:
            raise RenderError("encoded MP4 unexpectedly has audio")
        if float(media.get("format", {}).get("duration", 0)) <= 0:
            raise RenderError("encoded MP4 has no duration")
        if replace:
            temporary.replace(output)
        else:
            os.link(temporary, output)
    except OSError as error:
        if isinstance(error, FileExistsError):
            raise RenderError(f"MP4 already exists: {output}; use --replace to overwrite it") from error
        raise RenderError(f"cannot render MP4 {output}: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Existing MLT project")
    parser.add_argument("--output", type=Path, help="Output MP4 (default: project stem + .mp4)")
    parser.add_argument("--replace", action="store_true", help="Replace an existing MP4")
    parser.add_argument("--crf", type=int, default=23, help="H.264 quality, 0-51 (default: 23)")
    parser.add_argument("--preset", default="medium", help="H.264 speed preset (default: medium)")
    parser.add_argument("--audio-bitrate", default="192k", help="AAC bitrate (default: 192k)")
    args = parser.parse_args()
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")
    output = args.output or args.project.with_suffix(".mp4")
    if output.suffix.lower() != ".mp4":
        parser.error("--output must be an .mp4 file")
    try:
        render(args.project, output, args.replace, args.crf, args.preset, args.audio_bitrate)
    except RenderError as error:
        parser.error(str(error))
    print(f"MP4: {output}")


if __name__ == "__main__":
    main()
