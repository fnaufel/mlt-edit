#!/usr/bin/env python3
"""Convert a version 1 YAML edit plan to the version 2 segment format."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from csv_to_yaml import write_plan_yaml
from yaml_to_mlt import PlanError, position_seconds, probe_frame_rate


V1_SCHEMA_PATH = Path(__file__).with_name("edit-plan-v1.schema.json")


def migrate(data: Any, input_path: Path, output_path: Path) -> dict[str, Any]:
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise PlanError("input must be a version 1 YAML plan")
    schema = json.loads(V1_SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "plan"
        raise PlanError(f"{location}: {error.message}") from error

    raw_segments = data["segments"]
    source_path = Path(data["source"])
    if not source_path.is_absolute():
        source_path = input_path.parent / source_path
    has_frames = any(
        isinstance(raw.get(key), str) and raw[key].count(":") == 3
        for raw in raw_segments for key in ("source_start", "source_end")
    )
    frame_rate = probe_frame_rate(source_path) if has_frames else None
    previous_end = Decimal(0)
    segments = []
    for number, raw in enumerate(raw_segments, 1):
        start = position_seconds(raw["source_start"], f"segment {number} source_start", frame_rate)
        end = position_seconds(raw["source_end"], f"segment {number} source_end", frame_rate)
        if start != previous_end:
            raise PlanError(f"segment {number} must start at {previous_end} seconds to keep source-order contiguous intervals")
        if end <= start:
            raise PlanError(f"segment {number} must have positive duration (source_end > source_start)")
        keep = raw["keep"]
        join = raw.get("join_after")
        if not keep and join is not None:
            raise PlanError(f"segment {number} is discarded and cannot have a join_after")
        if keep and join == "dissolve":
            marker = "KEEP_TRANSITION"
            duration = raw.get("transition_duration")
            if duration is None:
                raise PlanError(f"segment {number} transition_duration is required for a dissolve")
            segment = {"source_end": raw["source_end"], "marker": marker, "transition_duration": duration}
        else:
            marker = "KEEP_CUT" if keep else "DELETE"
            segment = {"source_end": raw["source_end"], "marker": marker}
        segments.append(segment)
        previous_end = end

    result = {key: value for key, value in data.items() if key != "segments"}
    result["version"] = 2
    result["segments"] = segments
    for field in ("source", "audio_file"):
        if field in result and not Path(result[field]).is_absolute():
            original = (input_path.parent / result[field]).resolve()
            result[field] = os.path.relpath(original, output_path.parent.resolve())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Existing version 1 YAML plan")
    parser.add_argument("--output", type=Path, help="New version 2 plan (default: input stem + .v2.yaml)")
    parser.add_argument("--replace", action="store_true", help="Replace an existing version 2 output plan")
    args = parser.parse_args()
    output = args.output or args.plan.with_name(f"{args.plan.stem}.v2.yaml")
    if output.resolve() == args.plan.resolve():
        parser.error("output must differ from the original plan")
    if not args.replace and (output.exists() or output.is_symlink()):
        parser.error(f"plan already exists: {output}; use --replace to overwrite it")
    try:
        data = yaml.safe_load(args.plan.read_text(encoding="utf-8"))
        plan = migrate(data, args.plan, output)
        write_plan_yaml(plan, output, replace=args.replace)
    except (OSError, UnicodeError, yaml.YAMLError, PlanError, jsonschema.ValidationError) as error:
        parser.error(str(error))
    print(f"plan: {output}")


if __name__ == "__main__":
    main()
