# MLT edit plans

Convert one OBS marker CSV to an editable plan, generate production and preview MLT projects from the saved plan, then explicitly render the production project to MP4.

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

If the output plan already exists, the converter reports that before reading the config or CSV. Pass `--replace` to regenerate it intentionally.

### Convert the edit plan to MLT project files

```bash
uv run python yaml_to_mlt.py markers.plan.yaml
```

This command reads only the version 2 YAML plan and writes `markers.mlt` for rendering and `markers-preview.mlt` for inspecting cuts. Edit its `source` path, segment `marker` values, and `source_end` boundaries before generating the projects.

To convert a version 1 plan, run `uv run python migrate_plan.py markers.plan.yaml`. This writes `markers.plan.v2.yaml` and leaves the original untouched. Use `--output path/to/new-plan.yaml` to choose another location; source and replacement-audio paths are adjusted relative to it. An existing output is reported before the old plan is read. Use `--replace` only to replace that output. The migration checks the old segment boundaries and derives each new marker from `keep` and `join_after`, which were authoritative in version 1. If a descriptive old `marker` disagreed with those fields, the migrated marker follows the actual old edit decision. The project generator requires version 2 and points version 1 users to this command.

### Run from a recording directory

The launchers in `bin/` use this project's `.venv` and keep the current directory unchanged. Link them into a directory on your `PATH` (for example, `~/bin`), then run them beside the OBS CSV and recording:

```bash
mlt-edit-plan markers.csv
mlt-edit-migrate-plan old.plan.yaml
mlt-edit-project markers.plan.yaml
mlt-edit-render markers.mlt
```

`mlt-edit-plan` and `mlt-edit-project` use this repository's `edit-config.toml` by default. Pass `-c path/to/config.toml` to either command to override it. Relative input and output paths are resolved from the directory where you invoke each command.

The generated plan begins with a `# yaml-language-server: $schema=...` comment that points to [edit-plan.schema.json](edit-plan.schema.json). Install the Red Hat YAML extension in Positron to get validation and completion. The schema path is relative to the plan file. If you move the plan by itself, update that comment to point to the schema's new relative location.

In the plan:

- Each segment stores only `source_end`. Its start is zero for the first segment, then the previous segment's end. The converter writes quoted `HH:MM:SS` timecodes. You can add a frame number as `HH:MM:SS:FF`; the project generator uses the recording's frame rate to interpret it and rejects a frame number outside that rate. Fractional seconds remain valid as numbers for hand edits. The frame suffix is a frame offset within the named second, not a drop-frame timecode.
- Segment ends must increase strictly, so every interval has positive duration.
- `marker: KEEP_CUT` keeps the interval and cuts to the next kept interval; `KEEP_TRANSITION` keeps it and dissolves into the next kept interval; `DELETE` discards it. A transition requires `transition_duration`. The three marker names are fixed for edit boundaries; the config can still define point, range, or ignored annotation markers.

A `dissolve` joins the next kept segment when it is adjacent in source order. It overlaps the end of the first kept segment with the start of the next, shortens the output by the overlap, and crossfades the selected audio source. Conversion saves `transition_duration: 0.5` seconds by default, or the duration configured under `[behavior]`; edit this value on an individual segment to change its rendered join without reconverting the CSV.

If there is no selected audio source, project generation and MP4 rendering warn that no audio crossfade is possible. Terminal transitions and overlaps longer than either segment are rejected for now.

The source path is resolved relative to the plan. The generator checks the source's video frames, frame rate, resolution, duration, and audio streams.

When a source has multiple audio streams, set `audio_stream` in the plan to the desired stream index reported by `ffprobe`; a source without audio generates a silent project.

The production project defaults to the plan's basename with `.mlt`; the preview project adds `-preview` to that name. Use `--project path/to/output.mlt` or `--preview-project path/to/preview.mlt` to choose paths. The generator checks both paths before loading the plan or inspecting media. Neither project is replaced unless you pass `--replace`.

To replace OBS audio, add `audio_file: media/replacement.wav` to the hand-edited plan. The path is resolved relative to the plan, so it can be updated if the file moves. Its time zero aligns with the recording's time zero, and only the audio within kept source intervals is used. OBS audio is muted. If the external file has multiple audio streams, set `audio_stream` to the desired stream index in that file; otherwise the sole stream is selected automatically. The generator rejects a missing file, a file without audio, or a kept interval longer than the selected stream. Omit `audio_file` to retain OBS stream selection or silent-video behavior.

### Preview and refine the plan (optional)

Both MLT projects use the recording's detected profile. The preview shows one line along the bottom: `SOURCE: HH:MM:SS.SS    SEGMENT N        EDIT: HH:MM:SS.SS`. The source clock shows the original recording's elapsed time; the smaller edit clock shows the resulting timeline's elapsed time. The segment number is the corresponding 1-based entry in the YAML `segments` list. Deleted segments still count, so gaps in the preview's segment numbers point to those YAML entries. After a deleted interval, SOURCE jumps while EDIT continues. During a dissolve, the two source clock and segment label images overlap because both source frames are visible. Open Melt's interactive preview with:

```bash
melt markers-preview.mlt
```

When a boundary needs adjustment, change one `source_end` value in `markers.plan.yaml`, then regenerate both projects with `uv run python yaml_to_mlt.py markers.plan.yaml --replace` and preview again. This displays hundredths of a second; use a numeric fractional-second value in the YAML for finer adjustments. Interactive playback does not encode an output video.

Set preview typography and colors in `[preview]` in [edit-config.toml](edit-config.toml). The default is bold DejaVu Sans, yellow text, and fully opaque black backgrounds. `font_size = 72` is the source clock size at 1080p; sizes scale with video height, and the segment and edit text use 75% of that size. `background_opacity` ranges from `0` (transparent) to `1` (opaque). Colors use quoted `#RRGGBB` values. Only the preview project receives these overlays.

### Render the project

Project generation never starts an MP4 encode. Render `markers.mlt` to keep the preview clocks out of the final video. The render command requires an `audio_index` on each source producer so that it cannot choose a source audio stream implicitly. It defaults to `markers.mp4` beside `markers.mlt`, H.264 video (`libx264`, CRF 23, medium preset), and AAC audio at 192k when the project has audio.

It checks for an existing MP4 before inspecting the project or encoding; use `--replace` to request replacement. A failed render leaves any existing MP4 intact. The project remains separately protected by `yaml_to_mlt.py --replace`.

```bash
python3 render_mlt.py markers.mlt
```

To specify options:

```bash
python3 render_mlt.py markers.mlt --output edited.mp4 --crf 20 --preset fast --audio-bitrate 128k
```

`--crf` accepts 0 through 51, with lower values producing higher quality and larger files. `--preset` sets the x264 speed and compression tradeoff; `--audio-bitrate` sets the AAC bitrate. `render_mlt.py --help` lists all options.
