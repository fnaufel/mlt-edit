# MLT edit plans

Convert one OBS marker CSV to an editable plan, generate an MLT project from the saved plan, then explicitly render that project to MP4.

Python 3.11 or later, `ffprobe`, and `melt` are required. Install the Python dependencies with `uv sync`. The CLI tests also use `ffmpeg` to generate fixtures.

## Usage

### Record in OBS Studio using Local Stream Marker

Install [Local Stream Marker](https://github.com/honganqi/OBS-Local-Stream-Marker), then add `local-stream-marker.lua` in OBS under Tools → Scripts. Set its Output Folder for the CSV, enable Comments for markers, and create comments named `KEEP_CUT`, `KEEP_TRANSITION`, and `DELETE` to match [edit-config.toml](edit-config.toml). Reload the script and assign a hotkey to each comment under Settings → Hotkeys.

Record one video. Press a marker hotkey at the end of each interval:

- `KEEP_CUT` keeps the preceding interval and cuts to the next kept interval.
- `KEEP_TRANSITION` keeps the preceding interval and dissolves into the next kept interval.
- `DELETE` discards the preceding interval.

The plugin writes the marker CSV to its Output Folder. Footage after the last boundary marker is discarded, so mark the end of the final interval you want to keep. The example config also accepts `TITLE` and `IMPORTANT` as annotations in the YAML plan; they are not rendered yet.

### Convert the CSV file to a YAML edit plan

```bash
uv run python csv_to_yaml.py markers.csv -c edit-config.toml
```

### Convert the edit plan to an MLT project file

```bash
uv run python yaml_to_mlt.py markers.plan.yaml
```

This command reads only the YAML plan. If desired, edit its `source` path, `keep` decisions, and `source_start` or `source_end` values before generating the project.

### Run from a recording directory

The launchers in `bin/` use this project's `.venv` and keep the current directory unchanged. Link them into a directory on your `PATH` (for example, `~/bin`), then run them beside the OBS CSV and recording:

```bash
mlt-edit-plan markers.csv
mlt-edit-project markers.plan.yaml
mlt-edit-render markers.mlt
```

`mlt-edit-plan` uses this repository's `edit-config.toml` by default. Pass `-c path/to/config.toml` to override it. Relative input and output paths are resolved from the directory where you invoke each command.

The generated plan begins with a `# yaml-language-server: $schema=...` comment that points to [edit-plan.schema.json](edit-plan.schema.json). Install the Red Hat YAML extension in Positron to get validation and completion. The schema path is relative to the plan file. If you move the plan by itself, update that comment to point to the schema's new relative location.

In the plan:

- `source_start` and `source_end` are the only segment boundaries. The converter writes quoted `HH:MM:SS` timecodes. You can add a frame number as `HH:MM:SS:FF`; the project generator uses the recording's frame rate to interpret it and rejects a frame number outside that rate. Fractional seconds remain valid as numbers for hand edits. The frame suffix is a frame offset within the named second, not a drop-frame timecode.
- Segments must start at zero, remain contiguous and in source order, and have positive duration.
- A kept segment may use `join_after: cut`, `dissolve`, or `null`.

A `dissolve` joins the next kept segment when it is adjacent in source order. It overlaps the end of the first kept segment with the start of the next, shortens the output by the overlap, and crossfades the selected audio source. Conversion saves `transition_duration: 0.5` seconds by default, or the duration configured under `[behavior]`; edit this value on an individual segment to change its rendered join without reconverting the CSV.

If there is no selected audio source, project generation and MP4 rendering warn that no audio crossfade is possible. Terminal transitions and overlaps longer than either segment are rejected for now.

The source path is resolved relative to the plan. The generator checks the source's video frames, frame rate, resolution, duration, and audio streams.

When a source has multiple audio streams, set `audio_stream` in the plan to the desired stream index reported by `ffprobe`; a source without audio generates a silent project.

The project defaults to the plan's basename with `.mlt` and is protected from replacement. Use `--project path/to/output.mlt` to choose a path and `--replace` to replace an existing project.

To replace OBS audio, add `audio_file: media/replacement.wav` to the hand-edited plan. The path is resolved relative to the plan, so it can be updated if the file moves. Its time zero aligns with the recording's time zero, and only the audio within kept source intervals is used. OBS audio is muted. If the external file has multiple audio streams, set `audio_stream` to the desired stream index in that file; otherwise the sole stream is selected automatically. The generator rejects a missing file, a file without audio, or a kept interval longer than the selected stream. Omit `audio_file` to retain OBS stream selection or silent-video behavior.

### Preview the project (optional)

The MLT project uses the recording's detected profile. This command opens Melt's interactive preview:

```bash
melt markers.mlt
```

### Render the project

Project generation never starts an MP4 encode. The render command requires an `audio_index` on each source producer so that it cannot choose a source audio stream implicitly. It defaults to `markers.mp4` beside `markers.mlt`, H.264 video (`libx264`, CRF 23, medium preset), and AAC audio at 192k when the project has audio.

It refuses to overwrite an existing MP4; use `--replace` to request replacement. A failed render leaves any existing MP4 intact. The project remains separately protected by `yaml_to_mlt.py --replace`.

```bash
python3 render_mlt.py markers.mlt
```

To specify options:

```bash
python3 render_mlt.py markers.mlt --output edited.mp4 --crf 20 --preset fast --audio-bitrate 128k
```

`--crf` accepts 0 through 51, with lower values producing higher quality and larger files. `--preset` sets the x264 speed and compression tradeoff; `--audio-bitrate` sets the AAC bitrate. `render_mlt.py --help` lists all options.
