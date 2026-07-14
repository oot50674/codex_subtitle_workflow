#!/usr/bin/env python3
"""Add collision-free display padding to reviewed subtitle timings.

This utility does not infer timing corrections. It only expands decisions that
already contain an explicit ``timing`` override, then writes a new decisions
file for ``subflow.py apply``.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from subflow import (  # noqa: E402
    WorkflowError,
    load_manifest,
    utc_now,
    write_json_atomic,
)


PADDING_SCHEMA_VERSION = 1
ABSOLUTE_MAX_PADDING_MS = 3_000


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise WorkflowError(f"{label} must be an integer")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{label} must be an integer") from exc
    if str(value).strip() != str(converted) and not isinstance(value, int):
        raise WorkflowError(f"{label} must be an integer")
    return converted


def validate_padding(start_pad_ms: int, end_pad_ms: int, max_pad_ms: int) -> None:
    if not 0 <= max_pad_ms <= ABSOLUTE_MAX_PADDING_MS:
        raise WorkflowError(
            f"max padding must be between 0 and {ABSOLUTE_MAX_PADDING_MS} ms"
        )
    for label, value in (("start padding", start_pad_ms), ("end padding", end_pad_ms)):
        if not 0 <= value <= max_pad_ms:
            raise WorkflowError(f"{label} must be between 0 and {max_pad_ms} ms")


def _proportional_grants(gap_ms: int, left_request_ms: int, right_request_ms: int) -> tuple[int, int]:
    """Allocate one inter-cue gap without overlap, favoring neither side."""

    total_request = left_request_ms + right_request_ms
    if total_request == 0 or gap_ms <= 0:
        return 0, 0
    if total_request <= gap_ms:
        return left_request_ms, right_request_ms

    left = (gap_ms * left_request_ms + total_request // 2) // total_request
    left = max(gap_ms - right_request_ms, min(left_request_ms, left))
    right = gap_ms - left
    return left, right


def create_padded_decisions(
    manifest: dict[str, Any],
    decisions_payload: dict[str, Any],
    *,
    start_pad_ms: int = 300,
    end_pad_ms: int = 800,
    max_pad_ms: int = ABSOLUTE_MAX_PADDING_MS,
    created_at: str | None = None,
    allow_no_targets: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a padded decisions copy and a per-cue audit report."""

    validate_padding(start_pad_ms, end_pad_ms, max_pad_ms)
    if decisions_payload.get("timing_padding"):
        raise WorkflowError(
            "Decisions already contain timing_padding metadata; use the unpadded reviewed decisions"
        )

    cue_rows = manifest.get("cues")
    decision_rows = decisions_payload.get("decisions")
    if not isinstance(cue_rows, list) or not cue_rows:
        raise WorkflowError("Manifest has no cues")
    if not isinstance(decision_rows, list):
        raise WorkflowError("Decisions payload has no decisions list")

    decisions_by_index: dict[int, dict[str, Any]] = {}
    for item in decision_rows:
        if not isinstance(item, dict):
            raise WorkflowError("Every decision must be an object")
        index = _integer(item.get("index"), "decision index")
        if index in decisions_by_index:
            raise WorkflowError(f"Duplicate decision for cue {index}")
        decisions_by_index[index] = item

    base: list[dict[str, int]] = []
    targets: set[int] = set()
    seen_cues: set[int] = set()
    for row in cue_rows:
        if not isinstance(row, dict):
            raise WorkflowError("Every manifest cue must be an object")
        index = _integer(row.get("index"), "cue index")
        if index in seen_cues:
            raise WorkflowError(f"Duplicate manifest cue {index}")
        seen_cues.add(index)
        decision = decisions_by_index.get(index)
        if decision is None:
            raise WorkflowError(f"Missing decision for cue {index}")

        start_ms = _integer(row.get("start_ms"), f"cue {index} start_ms")
        end_ms = _integer(row.get("end_ms"), f"cue {index} end_ms")
        if "timing" in decision:
            timing = decision["timing"]
            if not isinstance(timing, dict) or set(timing) < {"start_ms", "end_ms"}:
                raise WorkflowError(
                    f"Cue {index} timing override must contain start_ms and end_ms"
                )
            start_ms = _integer(timing["start_ms"], f"cue {index} timing start_ms")
            end_ms = _integer(timing["end_ms"], f"cue {index} timing end_ms")
            targets.add(index)
        if start_ms < 0 or end_ms <= start_ms:
            raise WorkflowError(f"Cue {index} has invalid base timing: {start_ms}..{end_ms}")
        base.append({"index": index, "start_ms": start_ms, "end_ms": end_ms})

    extras = set(decisions_by_index) - seen_cues
    if extras:
        raise WorkflowError(f"Decisions reference unknown cues: {sorted(extras)[:10]}")
    if not targets and not allow_no_targets:
        raise WorkflowError("No explicit timing overrides were found")

    previous: dict[str, int] | None = None
    for cue in base:
        if previous is not None and cue["start_ms"] < previous["end_ms"]:
            raise WorkflowError(
                f"Base timing overlaps between cues {previous['index']} and {cue['index']}"
            )
        previous = cue

    duration_value = manifest.get("media", {}).get("format", {}).get("duration")
    try:
        duration_seconds = float(duration_value)
        if not math.isfinite(duration_seconds):
            raise ValueError("non-finite duration")
        media_duration_ms = round(duration_seconds * 1000)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("Manifest media duration is required for final timing padding") from exc
    if media_duration_ms <= 0 or base[-1]["end_ms"] > media_duration_ms:
        raise WorkflowError("Manifest media duration is shorter than the reviewed cue timeline")

    start_grants = {cue["index"]: 0 for cue in base}
    end_grants = {cue["index"]: 0 for cue in base}
    if base[0]["index"] in targets:
        start_grants[base[0]["index"]] = min(start_pad_ms, base[0]["start_ms"])

    for left, right in zip(base, base[1:]):
        gap_ms = right["start_ms"] - left["end_ms"]
        left_request = end_pad_ms if left["index"] in targets else 0
        right_request = start_pad_ms if right["index"] in targets else 0
        left_grant, right_grant = _proportional_grants(
            gap_ms, left_request, right_request
        )
        end_grants[left["index"]] = left_grant
        start_grants[right["index"]] = right_grant

    if base[-1]["index"] in targets:
        remaining_ms = media_duration_ms - base[-1]["end_ms"]
        end_grants[base[-1]["index"]] = min(end_pad_ms, remaining_ms)

    output_payload = copy.deepcopy(decisions_payload)
    output_map = {int(item["index"]): item for item in output_payload["decisions"]}
    cue_reports: list[dict[str, Any]] = []
    fully_applied = 0
    reduced = 0
    for cue in base:
        index = cue["index"]
        if index not in targets:
            continue
        applied_start = start_grants[index]
        applied_end = end_grants[index]
        padded_start = cue["start_ms"] - applied_start
        padded_end = cue["end_ms"] + applied_end
        output_map[index]["timing"] = {
            "start_ms": padded_start,
            "end_ms": padded_end,
        }
        is_reduced = applied_start < start_pad_ms or applied_end < end_pad_ms
        if is_reduced:
            reduced += 1
        else:
            fully_applied += 1
        cue_reports.append(
            {
                "index": index,
                "reviewed_timing_ms": [cue["start_ms"], cue["end_ms"]],
                "padded_timing_ms": [padded_start, padded_end],
                "requested_padding_ms": {
                    "start": start_pad_ms,
                    "end": end_pad_ms,
                },
                "applied_padding_ms": {
                    "start": applied_start,
                    "end": applied_end,
                },
                "reduced_to_avoid_collision_or_media_boundary": is_reduced,
            }
        )

    timestamp = created_at or utc_now()
    metadata = {
        "schema_version": PADDING_SCHEMA_VERSION,
        "created_at": timestamp,
        "policy": "reviewed overrides only; collision-free proportional gap allocation",
        "requested_start_pad_ms": start_pad_ms,
        "requested_end_pad_ms": end_pad_ms,
        "maximum_allowed_pad_ms": max_pad_ms,
        "target_count": len(targets),
        "fully_applied_count": fully_applied,
        "reduced_count": reduced,
    }
    output_payload["timing_padding"] = metadata
    report = {
        "schema_version": PADDING_SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "created_at": timestamp,
        "timing_changes_inferred": False,
        "untargeted_cues_changed": False,
        "media_duration_ms": media_duration_ms,
        "settings": {
            "start_pad_ms": start_pad_ms,
            "end_pad_ms": end_pad_ms,
            "max_pad_ms": max_pad_ms,
            "gap_policy": "proportional reduction when adjacent requests exceed the available gap",
        },
        "summary": {
            "cue_count": len(base),
            "target_count": len(targets),
            "fully_applied_count": fully_applied,
            "reduced_count": reduced,
        },
        "cues": cue_reports,
    }
    return output_payload, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add collision-free display padding to reviewed timing overrides"
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output-decisions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--start-pad-ms", type=int, default=300)
    parser.add_argument("--end-pad-ms", type=int, default=800)
    parser.add_argument("--max-pad-ms", type=int, default=ABSOLUTE_MAX_PADDING_MS)
    parser.add_argument(
        "--allow-no-targets",
        action="store_true",
        help="render a verified unchanged comparison when no timing overrides are present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve()
    decisions_path = args.decisions.expanduser().resolve()
    output_path = args.output_decisions.expanduser().resolve()
    report_path = args.report.expanduser().resolve()
    try:
        if output_path == report_path:
            raise WorkflowError("Output decisions and report paths must be different")
        if output_path in {manifest_path, decisions_path} or report_path in {
            manifest_path,
            decisions_path,
        }:
            raise WorkflowError("Padding outputs must not replace the manifest or reviewed decisions")
        for path in (output_path, report_path):
            if path.exists():
                raise WorkflowError(f"Refusing to overwrite existing padding output: {path}")

        manifest = load_manifest(manifest_path)
        decisions_payload = json.loads(decisions_path.read_text(encoding="utf-8-sig"))
        output_payload, report = create_padded_decisions(
            manifest,
            decisions_payload,
            start_pad_ms=args.start_pad_ms,
            end_pad_ms=args.end_pad_ms,
            max_pad_ms=args.max_pad_ms,
            allow_no_targets=args.allow_no_targets,
        )
        write_json_atomic(output_path, output_payload)
        write_json_atomic(report_path, report)
    except (OSError, ValueError, json.JSONDecodeError, WorkflowError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    summary = report["summary"]
    print(
        f"Padded {summary['target_count']} reviewed timings; "
        f"{summary['reduced_count']} required collision/boundary reduction"
    )
    print(f"Decisions: {output_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
