#!/usr/bin/env python3
"""
Convert an OBS Local Stream Marker CSV into:

1. a normalized JSON edit plan; and
2. a simple MLT XML project containing the kept intervals.

Marker semantics:
- A "boundary" marker describes the interval from the previous boundary
  (or recording start) up to this marker.
- Point/range markers do not move the previous edit boundary.

This first MLT backend implements KEEP/DELETE with hard cuts.
It preserves other semantics (e.g. transitions, titles, important ranges)
in the JSON plan for later backends.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tomllib
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any


def parse_hms(value: str) -> int:
    """Parse HH:MM:SS into integer seconds."""
    value = value.strip()
    if not value or value.lower() == "n/a":
        raise ValueError(f"not a timestamp: {value!r}")
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"expected HH:MM:SS, got {value!r}")
    h, m, s = map(int, parts)
    return h * 3600 + m * 60 + s


def seconds_to_hms(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def frame_at(seconds: int, fps: Fraction) -> int:
    # Markers only have one-second precision, so nearest frame is adequate.
    return round(seconds * fps)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f, skipinitialspace=True))


def build_plan(
    rows: list[dict[str, str]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    csv_cfg = cfg["csv"]
    marker_cfg = cfg["markers"]
    behavior = cfg.get("behavior", {})

    timestamp_col = csv_cfg["timestamp_column"]
    end_timestamp_col = csv_cfg.get("end_timestamp_column")
    path_col = csv_cfg["path_column"]
    comment_col = csv_cfg["comment_column"]

    if not rows:
        raise ValueError("CSV contains no marker rows")

    paths = {row[path_col].strip() for row in rows if row[path_col].strip()}
    if len(paths) != 1:
        raise ValueError(
            "This first version expects exactly one recording file; "
            f"found {len(paths)}: {sorted(paths)}"
        )
    source = next(iter(paths))

    events: list[dict[str, Any]] = []
    for row in rows:
        comment = row[comment_col].strip()
        if not comment:
            continue
        if comment not in marker_cfg:
            raise ValueError(f"Unknown marker comment {comment!r}")

        spec = marker_cfg[comment]
        kind = spec["kind"]
        t = parse_hms(row[timestamp_col])

        event: dict[str, Any] = {
            "time": t,
            "timecode": seconds_to_hms(t),
            "marker": comment,
            "kind": kind,
        }

        # Copy configured semantic fields into the normalized event.
        for key, value in spec.items():
            if key != "kind":
                event[key] = value

        if kind == "range" and end_timestamp_col:
            end_raw = row[end_timestamp_col].strip()
            if end_raw and end_raw.lower() != "n/a":
                end = parse_hms(end_raw)
                event["end"] = end
                event["end_timecode"] = seconds_to_hms(end)

        events.append(event)

    events.sort(key=lambda e: e["time"])

    segments: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    previous_boundary = 0

    for event in events:
        kind = event["kind"]

        if kind == "boundary":
            current = event["time"]
            if current < previous_boundary:
                raise ValueError("Markers are not in nondecreasing timestamp order")

            segment = {
                "source_start": previous_boundary,
                "source_end": current,  # half-open interval [start, end)
                "source_start_timecode": seconds_to_hms(previous_boundary),
                "source_end_timecode": seconds_to_hms(current),
                "keep": bool(event["keep"]),
                "marker": event["marker"],
                "join_after": event.get("join"),
            }
            segments.append(segment)
            previous_boundary = current

        elif kind in {"point", "range"}:
            annotations.append(event)

        else:
            raise ValueError(f"Unsupported marker kind {kind!r}")

    plan = {
        "version": 1,
        "source": source,
        "tail_policy": behavior.get("tail", "discard"),
        "segments": segments,
        "annotations": annotations,
    }
    return plan


def write_plan_json(plan: dict[str, Any], output: Path) -> None:
    output.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_mlt(plan: dict[str, Any], cfg: dict[str, Any], output: Path) -> None:
    media = cfg["media"]
    fps = Fraction(media["fps_num"], media["fps_den"])

    root = ET.Element("mlt")
    producer = ET.SubElement(root, "producer", {"id": "source"})
    ET.SubElement(producer, "property", {"name": "resource"}).text = plan["source"]

    playlist = ET.SubElement(root, "playlist", {"id": "main"})

    warnings: list[str] = []

    for seg in plan["segments"]:
        if not seg["keep"]:
            continue

        start_frame = frame_at(seg["source_start"], fps)
        end_frame = frame_at(seg["source_end"], fps)

        # MLT out points are inclusive. Our edit plan uses half-open intervals.
        out_frame = end_frame - 1
        if out_frame < start_frame:
            continue

        ET.SubElement(
            playlist,
            "entry",
            {
                "producer": "source",
                "in": str(start_frame),
                "out": str(out_frame),
            },
        )

        if seg.get("join_after") not in (None, "cut"):
            warnings.append(
                f"{seg['marker']} at {seg['source_end_timecode']} requests "
                f"join {seg['join_after']!r}; this first backend renders it as a hard cut."
            )

    tractor = ET.SubElement(root, "tractor", {"id": "main_tractor"})
    multitrack = ET.SubElement(tractor, "multitrack")
    ET.SubElement(multitrack, "track", {"producer": "main"})

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(output, encoding="utf-8", xml_declaration=True)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", type=Path)
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("edit-config.toml")
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="Output normalized JSON edit plan (default: CSV basename + .plan.json)",
    )
    parser.add_argument(
        "--mlt",
        type=Path,
        help="Output MLT XML (default: CSV basename + .mlt)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    rows = read_rows(args.csv_file)
    plan = build_plan(rows, cfg)

    stem = args.csv_file.with_suffix("")
    plan_path = args.plan or Path(f"{stem}.plan.json")
    mlt_path = args.mlt or Path(f"{stem}.mlt")

    write_plan_json(plan, plan_path)
    write_mlt(plan, cfg, mlt_path)

    print(f"source: {plan['source']}")
    print(f"segments: {len(plan['segments'])}")
    print(f"annotations: {len(plan['annotations'])}")
    print(f"plan: {plan_path}")
    print(f"mlt:  {mlt_path}")
    print()
    print("Kept intervals:")
    for seg in plan["segments"]:
        if seg["keep"]:
            print(
                f"  {seg['source_start_timecode']} -> "
                f"{seg['source_end_timecode']}  "
                f"({seg['marker']})"
            )


if __name__ == "__main__":
    main()
