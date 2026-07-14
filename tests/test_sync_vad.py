from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import subflow
from whisper_runtime import worker


class SyncVadAnalysisTests(unittest.TestCase):
    def analyze(self, cues, speech, **overrides):
        options = {
            "media_duration_ms": 3000,
            "boundary_search_ms": 750,
            "review_threshold_ms": 450,
            "low_overlap_ratio": 0.35,
            "no_overlap_ratio": 0.10,
            "utterance_join_gap_ms": 120,
            "orphan_speech_min_ms": 300,
        }
        options.update(overrides)
        return subflow.analyze_sync_intervals(cues, speech, **options)

    def test_exact_match_has_no_candidate(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 1000, "hello")],
            [(0, 1000)],
        )

        self.assertEqual(report["cues"][0]["candidate_reasons"], [])
        self.assertEqual(report["cues"][0]["candidate_score"], 0.0)
        self.assertEqual(report["candidates"], [])

    def test_subtitle_starting_before_speech_preserves_offset_sign(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 1000, "hello")],
            [(500, 1000)],
        )
        cue = report["cues"][0]
        reason_codes = {reason["code"] for reason in cue["candidate_reasons"]}

        self.assertIn("subtitle_starts_before_speech", reason_codes)
        self.assertEqual(cue["start_offset_ms"], -500)
        self.assertIn("start_timing_review", cue["flags"])

    def test_shared_utterance_is_group_context_not_a_false_error(self):
        report = self.analyze(
            [
                subflow.Cue(1, 0, 1000, "first"),
                subflow.Cue(2, 1000, 2000, "second"),
            ],
            [(0, 2000)],
        )

        self.assertEqual([cue["relation"] for cue in report["cues"]], [
            "one_speech_many_cues", "one_speech_many_cues",
        ])
        self.assertTrue(all(not cue["candidate_reasons"] for cue in report["cues"]))
        self.assertEqual(report["candidates"], [])
        self.assertFalse(report["utterance_groups"][0]["automatic_boundary_snap_allowed"])

    def test_no_speech_overlap_creates_explainable_high_priority_candidate(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 2000, "hello")],
            [(2500, 2800)],
        )
        cue = report["cues"][0]
        cue_candidate = next(
            item for item in report["candidates"] if item["candidate_id"] == "cue:1"
        )

        self.assertIn("no_detected_activity_overlap", cue["flags"])
        self.assertEqual(cue_candidate["severity"], "high")
        self.assertFalse(cue_candidate["automatic_timing_change_allowed"])
        self.assertEqual(
            cue_candidate["reasons"][0]["code"],
            "no_detected_speech_overlap",
        )

    def test_short_no_detected_speech_cue_is_capped_at_medium(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 800, "short interjection")],
            [(1500, 1800)],
        )
        candidate = next(
            item for item in report["candidates"] if item["candidate_id"] == "cue:1"
        )

        self.assertEqual(candidate["score"], 0.74)
        self.assertEqual(candidate["severity"], "medium")
        self.assertEqual(
            candidate["score_adjustments"][0]["code"],
            "short_cue_vad_false_negative_risk",
        )

    def test_no_overlap_uses_one_nearest_interval_as_context_not_boundary_evidence(self):
        report = self.analyze(
            [subflow.Cue(1, 1000, 1200, "gap")],
            [(100, 600), (1500, 1700)],
        )
        cue = report["cues"][0]

        self.assertEqual(cue["boundary_match_kind"], "nearest_nonoverlap_interval_context")
        self.assertEqual(cue["nearest_nonoverlap_interval_start_ms"], 1500)
        self.assertEqual(cue["nearest_nonoverlap_interval_end_ms"], 1700)
        self.assertFalse(cue["start_boundary_evidence"])
        self.assertFalse(cue["end_boundary_evidence"])

    def test_uncovered_speech_is_reported_without_mutating_cues(self):
        cues = [subflow.Cue(1, 0, 1000, "hello")]
        before = copy.deepcopy(cues)
        report = self.analyze(cues, [(0, 1000), (1500, 1900)])

        orphan = next(
            item for item in report["candidates"]
            if item["type"] == "speech_without_subtitle"
        )
        self.assertEqual((orphan["start_ms"], orphan["end_ms"]), (1500, 1900))
        self.assertEqual(cues, before)

    def test_many_speech_intervals_inside_one_cue_are_grouped(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 2000, "two phrases")],
            [(0, 500), (1000, 1500)],
        )

        self.assertEqual(report["cues"][0]["relation"], "many_speech_one_cue")
        self.assertIn("multiple_utterances_in_cue", report["cues"][0]["flags"])

    def test_boundary_does_not_pair_with_unrelated_nearby_speech(self):
        report = self.analyze(
            [subflow.Cue(1, 1000, 1500, "edge")],
            [(100, 1100), (1600, 1800)],
        )
        cue = report["cues"][0]

        self.assertEqual(cue["matched_speech_interval_indexes"], [1])
        self.assertIsNone(cue["detected_activity_start_ms"])
        self.assertFalse(cue["start_boundary_evidence"])

    def test_shared_speech_internal_boundary_is_not_snap_evidence(self):
        report = self.analyze(
            [
                subflow.Cue(1, 0, 1000, "first"),
                subflow.Cue(2, 1000, 1500, "second"),
            ],
            [(0, 1300)],
        )

        second = report["cues"][1]
        self.assertTrue(second["start_boundary_shared_with_previous_cue"])
        self.assertFalse(second["start_boundary_evidence"])
        self.assertIsNone(second["start_offset_ms"])

    def test_unrelated_preceding_speech_does_not_create_conflicting_start_reason(self):
        report = self.analyze(
            [subflow.Cue(1, 1000, 2000, "target")],
            [(500, 800), (1500, 1800)],
        )
        reason_codes = {
            reason["code"] for reason in report["cues"][0]["candidate_reasons"]
        }

        self.assertIn("subtitle_starts_before_speech", reason_codes)
        self.assertNotIn("subtitle_starts_after_uncovered_speech", reason_codes)

    def test_shared_utterance_tail_is_secondary_not_missing_subtitle(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 1000, "covered")],
            [(0, 1400)],
            media_duration_ms=2000,
        )

        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["summary"]["secondary_candidate_count"], 1)
        self.assertEqual(
            report["secondary_candidates"][0]["type"],
            "shared_utterance_uncovered_fragment",
        )

    def test_large_uncovered_tail_in_captioned_utterance_is_primary(self):
        report = self.analyze(
            [subflow.Cue(1, 0, 200, "tiny coverage")],
            [(0, 2000)],
            media_duration_ms=2500,
        )

        candidate = next(
            item for item in report["candidates"]
            if item["type"] == "possible_missing_or_shifted_subtitle"
        )
        self.assertGreaterEqual(candidate["score"], 0.45)
        self.assertEqual(
            candidate["reasons"][0]["code"],
            "large_uncovered_fragment_in_captioned_utterance",
        )

    def test_interval_normalization_is_deterministic(self):
        intervals = subflow.normalize_intervals_ms(
            [(250, 300), (100, 200), (200, 225)],
            duration_ms=1000,
            join_gap_ms=25,
        )

        self.assertEqual(intervals, [(100, 300)])
        serialized = json.dumps(intervals)
        self.assertEqual(serialized, "[[100, 300]]")

    def test_invalid_interval_is_rejected(self):
        with self.assertRaises(subflow.WorkflowError):
            subflow.normalize_intervals_ms([(500, 500)], duration_ms=1000)

    def test_non_finite_interval_is_rejected(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(subflow.WorkflowError):
                subflow.normalize_intervals_ms([(0, value)], duration_ms=1000)

    def test_ffmpeg_legacy_analysis_never_calls_activity_speech(self):
        analysis = subflow._legacy_activity_cue_analysis(
            [{"index": 1, "start_ms": 0, "end_ms": 1000}],
            [(0, 1000)],
            search_window_ms=750,
            review_threshold_ms=450,
        )

        self.assertIn("detected_activity_overlap_ratio", analysis[0])
        self.assertNotIn("vad_speech_overlap_ratio", analysis[0])

    def test_sync_refuses_to_overwrite_manifest_even_with_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            manifest = {
                "manifest_id": "test",
                "source": {
                    "video": str(root / "video.mp4"),
                    "subtitle": str(root / "source.srt"),
                },
                "artifacts": {"audio": None},
                "media": {"format": {"duration": "3.0"}},
                "cues": [],
            }
            args = SimpleNamespace(
                manifest=manifest_path,
                output=manifest_path,
                backend="silero",
                force=True,
            )

            with mock.patch.object(subflow, "load_manifest", return_value=manifest):
                with self.assertRaises(subflow.WorkflowError):
                    subflow.command_sync(args)

    def test_sync_force_refuses_to_overwrite_arbitrary_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            arbitrary = root / "decisions.final.json"
            arbitrary.write_text("not a sync report", encoding="utf-8")
            manifest = {
                "manifest_id": "test",
                "source": {
                    "video": str(root / "video.mp4"),
                    "subtitle": str(root / "source.srt"),
                },
                "artifacts": {"audio": None},
                "media": {"format": {"duration": "3.0"}},
                "cues": [],
            }
            args = SimpleNamespace(
                manifest=manifest_path,
                output=arbitrary,
                backend="silero",
                force=True,
            )

            with mock.patch.object(subflow, "load_manifest", return_value=manifest):
                with self.assertRaises(subflow.WorkflowError):
                    subflow.command_sync(args)

    def test_sync_rejects_non_finite_media_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            manifest = {
                "manifest_id": "test",
                "source": {
                    "video": str(root / "video.mp4"),
                    "subtitle": str(root / "source.srt"),
                },
                "artifacts": {"audio": None},
                "media": {"format": {"duration": math.inf}},
                "cues": [],
            }
            args = SimpleNamespace(
                manifest=manifest_path,
                output=root / "sync.json",
                backend="silero",
                force=False,
            )

            with mock.patch.object(subflow, "load_manifest", return_value=manifest):
                with self.assertRaises(subflow.WorkflowError):
                    subflow.command_sync(args)

    def test_sync_parser_keeps_legacy_backend_and_precise_silero_defaults(self):
        args = subflow.build_parser().parse_args([
            "sync", "--manifest", "manifest.json", "--output", "sync.json",
        ])

        self.assertEqual(args.backend, "ffmpeg")
        self.assertEqual(args.vad_threshold, 0.50)
        self.assertEqual(args.vad_neg_threshold, 0.35)
        self.assertEqual(args.vad_min_speech_ms, 100)
        self.assertEqual(args.vad_min_silence_ms, 250)
        self.assertEqual(args.vad_speech_pad_ms, 100)
        self.assertFalse(args.force)

    def test_worker_vad_parser_defaults(self):
        args = worker.build_parser().parse_args([
            "vad", "--input", "audio.wav", "--output-json", "vad.json",
        ])

        self.assertEqual(args.threshold, 0.5)
        self.assertIsNone(args.neg_threshold)
        self.assertEqual(args.min_speech_ms, 100)
        self.assertEqual(args.min_silence_ms, 250)
        self.assertEqual(args.speech_pad_ms, 100)

    def test_worker_vad_refuses_input_output_collision_before_loading_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "audio.wav"
            audio.write_bytes(b"not decoded because paths collide")
            args = worker.build_parser().parse_args([
                "vad", "--input", str(audio), "--output-json", str(audio),
            ])

            with self.assertRaises(ValueError):
                worker.command_vad(args)

    def test_parent_rejects_worker_audio_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.wav"
            audio.write_bytes(b"prepared audio fixture")
            manifest = {
                "source": {"video": str(root / "video.mp4")},
                "tools": {"ffmpeg": str(root / "ffmpeg.exe")},
                "artifacts": {
                    "audio": str(audio),
                    "audio_sha256": subflow.sha256(audio),
                },
            }
            args = SimpleNamespace(
                runtime_root=root,
                runtime_python=Path(sys.executable),
                vad_threshold=0.5,
                vad_neg_threshold=0.35,
                vad_min_speech_ms=100,
                vad_min_silence_ms=250,
                vad_speech_pad_ms=100,
                vad_max_speech_seconds=None,
            )

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-json") + 1])
                output.write_text(json.dumps({
                    "schema_version": subflow.SCHEMA_VERSION,
                    "input": {
                        "path": str(audio),
                        "sha256": "0" * 64,
                        "sampling_rate_hz": 16000,
                        "duration_ms": 1000,
                    },
                    "detector": {"kind": "silero-vad"},
                    "parameters": {
                        "threshold": 0.5,
                        "neg_threshold": 0.35,
                        "min_speech_duration_ms": 100,
                        "min_silence_duration_ms": 250,
                        "speech_pad_ms": 100,
                        "max_speech_duration_s": None,
                    },
                    "speech_intervals_ms": [],
                }), encoding="utf-8")

            with mock.patch.object(subflow, "run", side_effect=fake_run):
                with self.assertRaises(subflow.WorkflowError):
                    subflow._silero_vad_intervals(
                        manifest,
                        args,
                        duration_ms=1000,
                        temporary_root=root,
                    )

    def test_hash_bound_prepared_audio_may_omit_only_a_cue_free_damaged_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "audio.wav"
            audio.write_bytes(b"prepared audio fixture")
            audio_hash = subflow.sha256(audio)
            manifest = {
                "source": {"video": str(root / "video.mp4")},
                "tools": {"ffmpeg": str(root / "ffmpeg.exe")},
                "artifacts": {"audio": str(audio), "audio_sha256": audio_hash},
                "cues": [{"index": 1, "start_ms": 100, "end_ms": 700}],
            }
            args = SimpleNamespace(
                runtime_root=root,
                runtime_python=Path(sys.executable),
                vad_threshold=0.5,
                vad_neg_threshold=0.35,
                vad_min_speech_ms=100,
                vad_min_silence_ms=250,
                vad_speech_pad_ms=100,
                vad_max_speech_seconds=None,
                allow_truncated_audio_tail_ms=1300,
            )

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-json") + 1])
                output.write_text(json.dumps({
                    "schema_version": subflow.SCHEMA_VERSION,
                    "input": {
                        "path": str(audio),
                        "sha256": audio_hash,
                        "sampling_rate_hz": 16000,
                        "duration_ms": 800,
                    },
                    "detector": {"kind": "silero-vad"},
                    "parameters": {
                        "threshold": 0.5,
                        "neg_threshold": 0.35,
                        "min_speech_duration_ms": 100,
                        "min_silence_duration_ms": 250,
                        "speech_pad_ms": 100,
                        "max_speech_duration_s": None,
                    },
                    "speech_intervals_ms": [[200, 700]],
                }), encoding="utf-8")

            with mock.patch.object(subflow, "run", side_effect=fake_run):
                intervals, detector = subflow._silero_vad_intervals(
                    manifest,
                    args,
                    duration_ms=2000,
                    temporary_root=root,
                )
            self.assertEqual(intervals, [(200, 700)])
            self.assertTrue(detector["truncated_audio_tail"]["accepted"])
            self.assertEqual(detector["truncated_audio_tail"]["actual_missing_ms"], 1200)

            args.allow_truncated_audio_tail_ms = 0
            with mock.patch.object(subflow, "run", side_effect=fake_run):
                with self.assertRaisesRegex(subflow.WorkflowError, "duration differs"):
                    subflow._silero_vad_intervals(
                        manifest,
                        args,
                        duration_ms=2000,
                        temporary_root=root,
                    )


if __name__ == "__main__":
    unittest.main()
