#!/usr/bin/env python3
"""Convert an OBS Local Stream Marker CSV into an editable YAML edit plan.

Boundary markers classify the interval since the previous boundary or recording
start. Point and range markers remain annotations in source coordinates.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import jsonschema
import yaml


SCHEMA_PATH = Path(__file__).with_name("edit-plan.schema.json")
TIMECODE = re.compile(r"[0-9]{2,}:[0-5][0-9]:[0-5][0-9](?::[0-9]{2,})?")


class PlanDumper(yaml.SafeDumper):
    pass


def represent_string(dumper: PlanDumper, value: str) -> yaml.Node:
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style='"' if TIMECODE.fullmatch(value) else None
    )


PlanDumper.add_representer(str, represent_string)


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


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def read_rows(csv_path: Path) -> list[tuple[int, dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, skipinitialspace=True)
        return [(reader.line_num, row) for row in reader]


def build_plan(
    rows: list[tuple[int, dict[str, str]]],
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

    paths = {row[path_col].strip() for _, row in rows if row[path_col].strip()}
    if len(paths) != 1:
        raise ValueError(
            "This first version expects exactly one recording file; "
            f"found {len(paths)}: {sorted(paths)}"
        )
    source = next(iter(paths))

    events: list[dict[str, Any]] = []
    previous_time: int | None = None
    for row_number, row in rows:
        comment = row[comment_col].strip()
        if comment not in marker_cfg:
            raise ValueError(f"CSV row {row_number}: unknown marker comment {comment!r}")

        spec = marker_cfg[comment]
        kind = spec["kind"]
        t = parse_hms(row[timestamp_col])
        if previous_time is not None and t < previous_time:
            raise ValueError(f"CSV row {row_number}: timestamp is earlier than the previous row")
        previous_time = t
        if kind == "ignored":
            continue

        event: dict[str, Any] = {
            "time": t,
            "marker": comment,
            "kind": kind,
            "csv_row": row_number,
        }

        # Copy configured semantic fields into the normalized event.
        for key, value in spec.items():
            if key != "kind":
                event[key] = value

        if kind == "range" and end_timestamp_col:
            end_raw = row[end_timestamp_col].strip()
            if end_raw and end_raw.lower() != "n/a":
                end = parse_hms(end_raw)
                if end < t:
                    raise ValueError(f"CSV row {row_number}: range end precedes its start")
                event["end"] = end

        events.append(event)

    segments: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    previous_boundary = 0

    for event in events:
        kind = event["kind"]

        if kind == "boundary":
            current = event["time"]
            if segments and current == previous_boundary:
                raise ValueError(
                    f"CSV row {event['csv_row']}: duplicate edit boundary at {seconds_to_hms(current)}"
                )

            segment = {
                "source_start": seconds_to_hms(previous_boundary),
                "source_end": seconds_to_hms(current),  # half-open interval [start, end)
                "keep": bool(event["keep"]),
                "marker": event["marker"],
                "join_after": event.get("join"),
            }
            if segment["keep"] and segment["join_after"] not in (None, "cut"):
                segment["transition_duration"] = behavior.get(
                    "transition_duration", 0.5
                )
            segments.append(segment)
            previous_boundary = current

        elif kind in {"point", "range"}:
            event["time"] = seconds_to_hms(event["time"])
            if "end" in event:
                event["end"] = seconds_to_hms(event["end"])
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


def write_plan_yaml(plan: dict[str, Any], output: Path, replace: bool = False) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(plan, schema)
    schema_reference = os.path.relpath(SCHEMA_PATH, output.parent)
    with output.open("w" if replace else "x", encoding="utf-8") as file:
        file.write(f"# yaml-language-server: $schema={schema_reference}\n")
        yaml.dump(plan, file, Dumper=PlanDumper, sort_keys=False, allow_unicode=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", type=Path)
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("edit-config.toml")
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="Output editable YAML edit plan (default: CSV basename + .plan.yaml)",
    )
    parser.add_argument("--replace", action="store_true", help="Replace an existing plan")
    args = parser.parse_args()

    cfg = load_config(args.config)
    rows = read_rows(args.csv_file)
    try:
        plan = build_plan(rows, cfg)
    except ValueError as error:
        parser.error(str(error))

    stem = args.csv_file.with_suffix("")
    plan_path = args.plan or Path(f"{stem}.plan.yaml")
    source_path = Path(plan["source"])
    if not source_path.is_absolute():
        source_path = args.csv_file.parent / source_path
    try:
        plan["source"] = os.path.relpath(source_path, plan_path.parent)
    except ValueError:
        plan["source"] = str(source_path)

    try:
        write_plan_yaml(plan, plan_path, replace=args.replace)
    except FileExistsError:
        parser.error(f"plan already exists: {plan_path}; use --replace to overwrite it")
    except jsonschema.ValidationError as error:
        parser.error(f"generated plan is invalid: {error.message}")

    for annotation in plan["annotations"]:
        if annotation["kind"] == "range" and "end" not in annotation:
            print(
                f"warning: CSV row {annotation['csv_row']}: incomplete range "
                f"{annotation['marker']!r} has no captured end",
                file=sys.stderr,
            )

    print(f"source: {plan['source']}")
    print(f"segments: {len(plan['segments'])}")
    print(f"annotations: {len(plan['annotations'])}")
    print("annotations are preserved in source coordinates but not rendered in this milestone")
    print(f"plan: {plan_path}")
    print()
    print("Kept intervals:")
    for seg in plan["segments"]:
        if seg["keep"]:
            print(
                f"  {seg['source_start']} -> "
                f"{seg['source_end']}  "
                f"({seg['marker']})"
            )


if __name__ == "__main__":
    main()
