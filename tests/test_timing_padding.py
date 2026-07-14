from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "apply_timing_padding", ROOT / "scripts" / "apply_timing_padding.py"
)
assert SPEC and SPEC.loader
padding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(padding)


def manifest(cues: list[tuple[int, int, int]], duration_ms: int = 7_000) -> dict:
    return {
        "schema_version": 1,
        "manifest_id": "manifest-test",
        "media": {"format": {"duration": str(duration_ms / 1000)}},
        "cues": [
            {
                "index": index,
                "start_ms": start,
                "end_ms": end,
                "source": f"source {index}",
            }
            for index, start, end in cues
        ],
    }


def decisions(cues: list[tuple[int, int, int]], targets: set[int]) -> dict:
    rows = []
    for index, start, end in cues:
        row = {
            "index": index,
            "source": f"source {index}",
            "corrected": f"corrected {index}",
            "translation": f"translation {index}",
            "notes": f"note {index}",
        }
        if index in targets:
            row["timing"] = {"start_ms": start, "end_ms": end}
        rows.append(row)
    return {
        "schema_version": 1,
        "manifest_id": "manifest-test",
        "decisions": rows,
    }


class TimingPaddingTests(unittest.TestCase):
    def test_full_padding_only_changes_explicit_timing_target(self) -> None:
        cues = [(1, 1_000, 2_000), (2, 3_000, 4_000), (3, 5_000, 6_000)]
        source = decisions(cues, {2})
        result, report = padding.create_padded_decisions(
            manifest(cues), source, created_at="2026-01-01T00:00:00+00:00"
        )

        self.assertNotIn("timing", result["decisions"][0])
        self.assertEqual(
            result["decisions"][1]["timing"], {"start_ms": 2_700, "end_ms": 4_800}
        )
        self.assertNotIn("timing", result["decisions"][2])
        self.assertEqual(report["summary"]["target_count"], 1)
        self.assertEqual(report["summary"]["reduced_count"], 0)

    def test_adjacent_targets_share_short_gap_proportionally(self) -> None:
        cues = [(1, 1_000, 2_000), (2, 3_000, 4_000)]
        result, report = padding.create_padded_decisions(
            manifest(cues, duration_ms=5_000),
            decisions(cues, {1, 2}),
            created_at="2026-01-01T00:00:00+00:00",
        )

        first = result["decisions"][0]["timing"]
        second = result["decisions"][1]["timing"]
        self.assertEqual(first["end_ms"], second["start_ms"])
        self.assertEqual(first["end_ms"], 2_727)
        self.assertEqual(second["start_ms"], 2_727)
        self.assertEqual(report["summary"]["reduced_count"], 2)

    def test_single_side_receives_only_available_gap(self) -> None:
        cues = [(1, 1_000, 2_800), (2, 3_000, 4_000)]
        result, report = padding.create_padded_decisions(
            manifest(cues, duration_ms=5_000),
            decisions(cues, {2}),
            created_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(result["decisions"][1]["timing"]["start_ms"], 2_800)
        self.assertEqual(report["cues"][0]["applied_padding_ms"]["start"], 200)

    def test_media_boundaries_clip_padding(self) -> None:
        cues = [(1, 100, 1_000), (2, 1_500, 1_900)]
        result, report = padding.create_padded_decisions(
            manifest(cues, duration_ms=2_000),
            decisions(cues, {1, 2}),
            created_at="2026-01-01T00:00:00+00:00",
        )

        self.assertEqual(result["decisions"][0]["timing"]["start_ms"], 0)
        self.assertEqual(result["decisions"][1]["timing"]["end_ms"], 2_000)
        self.assertEqual(report["cues"][0]["applied_padding_ms"]["start"], 100)
        self.assertEqual(report["cues"][1]["applied_padding_ms"]["end"], 100)

    def test_text_translation_and_notes_are_unchanged(self) -> None:
        cues = [(1, 1_000, 2_000)]
        source = decisions(cues, {1})
        result, _ = padding.create_padded_decisions(
            manifest(cues, duration_ms=3_000),
            source,
            created_at="2026-01-01T00:00:00+00:00",
        )

        for key in ("source", "corrected", "translation", "notes"):
            self.assertEqual(result["decisions"][0][key], source["decisions"][0][key])

    def test_overlapping_reviewed_timing_is_rejected(self) -> None:
        cues = [(1, 1_000, 2_000), (2, 3_000, 4_000)]
        source = decisions(cues, {1, 2})
        source["decisions"][0]["timing"]["end_ms"] = 3_100

        with self.assertRaisesRegex(padding.WorkflowError, "Base timing overlaps"):
            padding.create_padded_decisions(manifest(cues), source)

    def test_double_padding_is_rejected(self) -> None:
        cues = [(1, 1_000, 2_000)]
        source = decisions(cues, {1})
        source["timing_padding"] = {"schema_version": 1}

        with self.assertRaisesRegex(padding.WorkflowError, "already contain timing_padding"):
            padding.create_padded_decisions(manifest(cues), source)

    def test_explicit_unchanged_comparison_has_zero_targets(self) -> None:
        cues = [(1, 1_000, 2_000)]
        source = decisions(cues, set())
        result, report = padding.create_padded_decisions(
            manifest(cues, duration_ms=3_000),
            source,
            allow_no_targets=True,
            created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertNotIn("timing", result["decisions"][0])
        self.assertEqual(report["summary"]["target_count"], 0)

    def test_non_finite_media_duration_is_rejected(self) -> None:
        cues = [(1, 1_000, 2_000)]
        for value in ("nan", "inf", "-inf"):
            bad_manifest = manifest(cues)
            bad_manifest["media"]["format"]["duration"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                padding.WorkflowError, "media duration is required"
            ):
                padding.create_padded_decisions(bad_manifest, decisions(cues, {1}))

    def test_padding_above_three_seconds_is_rejected(self) -> None:
        cues = [(1, 4_000, 5_000)]
        with self.assertRaisesRegex(padding.WorkflowError, "between 0 and 3000"):
            padding.create_padded_decisions(
                manifest(cues, duration_ms=10_000),
                decisions(cues, {1}),
                start_pad_ms=3_001,
            )


if __name__ == "__main__":
    unittest.main()
