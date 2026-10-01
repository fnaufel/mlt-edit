# MLT edit plans

Convert one OBS marker CSV to an editable plan, then generate a cut-only MLT project from the saved plan. Python 3.11 or later, `ffprobe`, and `melt` are required for project generation. `ffmpeg` is used by the CLI tests to generate fixtures.

```bash
python3 obs_csv_to_mlt.py markers.csv -c edit-config.toml
python3 plan_to_mlt.py markers.plan.json
melt markers.mlt
```

The second command reads only the JSON plan. Edit its `source` path, `keep` decisions, and `source_start` or `source_end` values before generating the project. Times are seconds and can have fractional values. Segments must start at zero, remain contiguous and in source order, and have positive duration. Each kept segment may have `join_after: "cut"` or `null`. A `dissolve` request gives an error until transition rendering is implemented.

The source path is resolved relative to the plan. The generator checks the source's video frames, frame rate, resolution, duration, and audio streams. When a source has multiple audio streams, set `audio_stream` in the plan to the desired stream index reported by `ffprobe`; a source without audio generates a silent project. The project defaults to the plan's basename with `.mlt` and is protected from replacement. Use `--project path/to/output.mlt` to choose a path and `--replace` to replace an existing project.

The MLT project uses the recording's detected profile and can be previewed with `melt` or rendered explicitly, for example:

```bash
melt markers.mlt -consumer avformat:edited.mp4 vcodec=libx264 acodec=aac
```
