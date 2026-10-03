# Specification

## First version

I suggest **not** going directly from the OBS CSV to complicated MLT XML. Put a small, human-readable semantic layer in between:

```text
OBS Local Stream Marker CSV
        ↓
csv_to_json.py
        ↓
normalized edit plan (JSON)
        ↓
json_to_mlt.py
        ↓
MLT XML
        ↓
melt → final video
```

I made a working first version against your actual CSV:

- `csv_to_json.py`
- `json_to_mlt.py`
- `edit-config.toml`

### What your test becomes

Your CSV contains:

```text
00:00:39  KEEP_CUT
00:00:47  KEEP_CUT
00:00:55  DELETE
00:01:01  KEEP_CUT
```

The normalized plan is therefore:

```text
00:00 → 00:39    KEEP, hard cut
00:39 → 00:47    KEEP, hard cut
00:47 → 00:55    DELETE
00:55 → 01:01    KEEP, hard cut
```

The generated MLT playlist contains only the three surviving ranges.

At \(30\) fps it currently produces:

```xml
<playlist id="main">
  <entry producer="source" in="0"    out="1169" />
  <entry producer="source" in="1170" out="1409" />
  <entry producer="source" in="1650" out="1829" />
</playlist>
```

MLT playlist `in`/`out` points are absolute frame positions relative to the producer, and the `out` point is inclusive. The [MLT XML documentation](https://www.mltframework.org/docs/mltxml/) shows a 3000-frame entry ending at frame 2999. That's why the program internally treats your intervals as half-open intervals such as

\[
[0,39)
\]

and translates that at \(30\) fps into frames \(0\) through \(1169\), rather than through \(1170\).

### Configuration

The interesting part is deliberately outside the Python program:

```toml
[markers.KEEP_CUT]
kind = "boundary"
keep = true
join = "cut"

[markers.KEEP_TRANSITION]
kind = "boundary"
keep = true
join = "dissolve"

[markers.DELETE]
kind = "boundary"
keep = false

[markers.TITLE]
kind = "point"
effect = "title"
default_duration = 5

[markers.IMPORTANT]
kind = "range"
role = "important"
```

This is what I mean by configurable but simple. The Python code should not contain things like:

```python
if comment == "KEEP_CUT":
```

scattered through the program. It should know about semantic concepts such as `boundary`, `point`, `range`, `keep`, `join`, etc.; the TOML says what your OBS marker names mean.

Python \(3.11+\) reads TOML with `tomllib`, so there is no PyYAML or other dependency.

### The normalized plan is important

For your test it produces this, in part:

```json
{
  "source": "/path/to/recording.mkv",
  "segments": [
    {
      "source_start": 0,
      "source_end": 39,
      "keep": true,
      "marker": "KEEP_CUT",
      "join_after": "cut"
    },
    {
      "source_start": 39,
      "source_end": 47,
      "keep": true,
      "marker": "KEEP_CUT",
      "join_after": "cut"
    },
    {
      "source_start": 47,
      "source_end": 55,
      "keep": false,
      "marker": "DELETE"
    }
  ]
}
```

I would make **this JSON format the contract of your editing system**.

That has a major advantage later. You can have:

```text
OBS CSV ──→ Edit Plan ──→ MLT
                     ├──→ FFmpeg
                     ├──→ Kdenlive project
                     └──→ diagnostic timeline
```

And changing the OBS hotkeys won't require rewriting the MLT implementation.

### Running it

With the scripts and config in the repo directory:

```bash
python3 csv_to_json.py \
    2026-09-30_17-31-50.csv \
    -c edit-config.toml
python3 json_to_mlt.py 2026-09-30_17-31-50.plan.json
```

It produces:

```text
2026-09-30_17-31-50.plan.json
2026-09-30_17-31-50.mlt
```

For your file, it prints:

```text
Kept intervals:
  00:00:00 -> 00:00:39  (KEEP_CUT)
  00:00:39 -> 00:00:47  (KEEP_CUT)
  00:00:55 -> 00:01:01  (KEEP_CUT)
```

You could preview the MLT project with something along the lines of:

```bash
melt -profile atsc_1080p_30 \
    2026-09-30_17-31-50.mlt
```

and render with:

```bash
melt -profile atsc_1080p_30 \
    2026-09-30_17-31-50.mlt \
    -consumer avformat:edited.mp4
```

MLT profiles define resolution, frame rate, aspect ratio, etc.; `atsc_1080p_30` is one of the supplied MLT profiles. See the [MLT profiles documentation](https://www.mltframework.org/docs/profiles/). Change the TOML values if your OBS recording is \(60\) fps, \(29.97\) fps, etc.

### What I intentionally haven't implemented yet

The current MLT backend fully implements **KEEP_CUT** and **DELETE**. It understands `KEEP_TRANSITION`, `TITLE`, and `IMPORTANT` semantically and preserves them in the plan, but it does not yet render them.

That's intentional rather than trying to solve everything in version 1.

A dissolve requires us to move from one simple MLT playlist to overlapping clips on multiple tracks and apply video/audio transitions. MLT's [mix documentation](https://www.mltframework.org/docs/melt/) demonstrates `luma` for the video dissolve and `mix` for audio.

Similarly, `TITLE` should become an overlay track, while `IMPORTANT` should probably remain metadata unless you decide exactly what an “important segment” should do.

### One choice I made

The config currently says:

```toml
[behavior]
tail = "discard"
```

So anything after the last boundary marker is discarded. That matches the idea that you explicitly press `+`, `-`, or transition to classify each interval.

We could alternatively make recording stop implicitly mean:

```text
KEEP from final marker → end of recording
```

but I prefer your current explicit model. It makes the recorded decisions deterministic.

Also, I'm using **`Recording Timestamp on File`**, not simply `Recording Timestamp`. [Local Stream Marker documents](https://github.com/honganqi/OBS-Local-Stream-Marker/blob/main/README.md) that the former accounts for OBS Automatic File Splitting; they differ only when splitting is active. The current converter still deliberately requires one recording file; multi-file recordings can be the next extension.

The next thing I would implement is **`KEEP_TRANSITION`**, because that will force us to settle the general MLT timeline representation. The agreed milestone below now also includes replacement audio; rendering `TITLE` and processing audio remain later work.

## Agreed next milestone

These confirmed decisions extend the first version above.

### Workflow and artifacts

- Convert an OBS Local Stream Marker CSV into an independently editable JSON edit plan. Conversion can run without the video file and does not overwrite an existing plan unless explicitly requested.
- Render from the JSON plan without reading the CSV. The plan carries resolved transition durations, so later configuration changes do not alter an existing plan.
- Use one source video file. Refer to it relatively when possible and allow its path to be corrected after moving the project.
- Generate an inspectable MLT project for `melt` preview and an MP4 video through separate, explicit steps. Kdenlive project compatibility is outside this milestone. Neither output overwrites an existing file without an explicit request.
- Default to H.264 video and AAC audio, with configurable encoding settings.

### Timeline and markers

- A boundary marker classifies the interval since the preceding boundary or the start of the recording. The portion after the final boundary is discarded.
- Keep all classified segments in the plan, including discarded ones. They cover the classified portion from time zero without gaps or overlaps and remain in source order. Hand edits may move or add boundaries with fractional-second precision, trim, and split segments; reordering and repeating footage are outside this milestone.
- Reject two boundary markers at the same second, out-of-order CSV timestamps, and unknown marker comments. An explicitly configured ignored marker has no editing effect. Errors identify the relevant CSV rows.
- Conversion may save a plan with no kept segments, but rendering that plan fails with a clear error.

### Joins and media

- A kept segment's outgoing join applies to the next kept segment even when discarded footage separates them. A cut joins without overlap; a transition dissolves video and crossfades the selected audio track.
- The default transition duration is 0.5 seconds and is resolved into the plan. A transition overlaps the end of the first kept segment with the start of the next, shortens the output by its duration, and never includes discarded footage. A hand-edited plan may override the duration for an individual join.
- Reject a transition without a following kept segment, one longer than either adjacent segment, or two transitions whose combined overlap exceeds the length of their shared segment.
- Inspect the source recording at rendering time for frame rate, resolution, duration, and audio tracks. Reject irregular frame timing before the end of the last kept segment, but ignore gaps wholly within the discarded tail after that point; reject plan intervals beyond the recording. If OBS audio is used and there are several audio tracks, the plan must select one. If no audio source is selected, render the video dissolve and report that audio crossfading was omitted.
- The plan may instead select one separate audio file as the audio source, replacing all OBS audio. Align its time zero with the source recording's time zero, apply the same kept intervals and transition overlaps to its audio, and never mix in OBS audio. Resolve its path relative to the plan where possible and allow the path to be corrected after a move. Reject a missing file, a file without audio, or a kept interval beyond its duration. If it has multiple audio streams, require an explicit stream selection. No sync offset or audio processing is part of this milestone.

### Annotations

- Preserve `TITLE` points and `IMPORTANT` ranges in source-recording coordinates even when they fall in discarded footage. Report that they are not rendered in this milestone.
- Preserve an `IMPORTANT` range with no end as incomplete and warn. Reject a range whose end precedes its start, identifying the CSV row.
