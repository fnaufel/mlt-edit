# MLT edit plans

Convert one OBS marker CSV to an editable plan, generate an MLT project from the saved plan, then explicitly render that project to MP4. Python 3.11 or later, `ffprobe`, and `melt` are required. `ffmpeg` is used by the CLI tests to generate fixtures.

```bash
python3 obs_csv_to_mlt.py markers.csv -c edit-config.toml
python3 plan_to_mlt.py markers.plan.json
melt markers.mlt
python3 render_mlt.py markers.mlt
```

The second command reads only the JSON plan. Edit its `source` path, `keep` decisions, and `source_start` or `source_end` values before generating the project. Times are seconds and can have fractional values. Segments must start at zero, remain contiguous and in source order, and have positive duration. A kept segment may use `join_after: "cut"`, `"dissolve"`, or `null`.

A `dissolve` joins the next kept segment when it is adjacent in source order. It overlaps the end of the first kept segment with the start of the next, shortens the output by the overlap, and crossfades the selected audio source. Conversion saves `transition_duration: 0.5` seconds by default, or the duration configured under `[behavior]`; edit this value on an individual segment to change its rendered join without reconverting the CSV. If there is no selected audio source, project generation and MP4 rendering warn that no audio crossfade is possible. Terminal transitions, overlaps longer than either segment, transitions across discarded footage, and multiple transitions are rejected for now.

The source path is resolved relative to the plan. The generator checks the source's video frames, frame rate, resolution, duration, and audio streams. When a source has multiple audio streams, set `audio_stream` in the plan to the desired stream index reported by `ffprobe`; a source without audio generates a silent project. The project defaults to the plan's basename with `.mlt` and is protected from replacement. Use `--project path/to/output.mlt` to choose a path and `--replace` to replace an existing project.

To replace OBS audio, add `"audio_file": "media/replacement.wav"` to the hand-edited plan. The path is resolved relative to the plan, so it can be updated if the file moves. Its time zero aligns with the recording's time zero, and only the audio within kept source intervals is used. OBS audio is muted. If the external file has multiple audio streams, set `audio_stream` to the desired stream index in that file; otherwise the sole stream is selected automatically. The generator rejects a missing file, a file without audio, or a kept interval longer than the selected stream. Omit `audio_file` to retain OBS stream selection or silent-video behavior.

The MLT project uses the recording's detected profile and can be previewed with `melt`. Project generation never starts an MP4 encode. The render command requires an `audio_index` on each source producer so that it cannot choose a source audio stream implicitly. It defaults to `markers.mp4` beside `markers.mlt`, H.264 video (`libx264`, CRF 23, medium preset), and AAC audio at 192k when the project has audio. It refuses to overwrite an existing MP4; use `--replace` to request replacement. A failed render leaves any existing MP4 intact. The project remains separately protected by `plan_to_mlt.py --replace`.

```bash
python3 render_mlt.py markers.mlt --output edited.mp4 --crf 20 --preset fast --audio-bitrate 128k
```

`--crf` accepts 0 through 51, with lower values producing higher quality and larger files. `--preset` sets the x264 speed and compression tradeoff; `--audio-bitrate` sets the AAC bitrate. `render_mlt.py --help` lists all options.
