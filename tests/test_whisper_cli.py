from __future__ import annotations

import unittest
import tempfile
import sys
import types
from pathlib import Path
from unittest import mock

import subflow
from whisper_runtime import worker


class WhisperCliTests(unittest.TestCase):
    def test_parse_time_point(self) -> None:
        self.assertEqual(subflow.parse_time_point("12.5"), 12_500)
        self.assertEqual(subflow.parse_time_point("02:03.250"), 123_250)
        self.assertEqual(subflow.parse_time_point("01:02:03,004"), 3_723_004)

    def test_invalid_time_point(self) -> None:
        with self.assertRaises(subflow.WorkflowError):
            subflow.parse_time_point("00:61")

    def test_default_runtime_is_project_local(self) -> None:
        runtime = subflow.whisper_runtime_python()
        self.assertIn(subflow.PROJECT_ROOT, runtime.parents)
        self.assertIn(".runtime", runtime.parts)

    def test_transcribe_defaults(self) -> None:
        args = subflow.build_parser().parse_args([
            "transcribe", "video.mp4", "--output", "draft.srt",
        ])
        self.assertEqual(args.model, "large-v3-turbo")
        self.assertEqual(args.device, "cuda")
        self.assertEqual(args.compute_type, "float16")
        self.assertEqual(args.model_root, subflow.DEFAULT_WHISPER_MODEL_ROOT)
        self.assertEqual(args.output, Path("draft.srt"))

    def test_source_sidecar_naming(self) -> None:
        path = subflow.subtitle_sidecar_path(Path("A Diabolical.mp4"), "ko")
        self.assertEqual(path.name, "A Diabolical.ko.srt")

    def test_atomic_sidecar_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "translated.ko.srt"
            destination = root / "video.ko.srt"
            source.write_text("subtitle", encoding="utf-8")
            subflow.copy_file_atomic(source, destination)
            self.assertEqual(destination.read_text(encoding="utf-8"), "subtitle")

    def test_worker_prefers_project_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "large-v3-turbo"
            model.mkdir()
            (model / "model.bin").write_bytes(b"model")
            (model / "config.json").write_text("{}", encoding="utf-8")
            resolved, was_cached = worker.ensure_model_reference(
                "large-v3-turbo", Path(temporary), local_files_only=True,
            )
            self.assertEqual(Path(resolved), model.resolve())
            self.assertTrue(was_cached)

    def test_worker_offline_mode_rejects_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                worker.ensure_model_reference(
                    "large-v3-turbo", Path(temporary), local_files_only=True,
                )

    def test_worker_downloads_missing_model_into_project_cache(self) -> None:
        calls = []
        fake_package = types.ModuleType("faster_whisper")
        fake_package.__path__ = []
        fake_utils = types.ModuleType("faster_whisper.utils")

        def fake_download_model(model, *, output_dir, local_files_only, cache_dir):
            calls.append((model, output_dir, local_files_only, cache_dir))
            target = Path(output_dir)
            target.mkdir(parents=True)
            (target / "model.bin").write_bytes(b"downloaded")
            (target / "config.json").write_text("{}", encoding="utf-8")
            return str(target)

        fake_utils.download_model = fake_download_model
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            sys.modules,
            {"faster_whisper": fake_package, "faster_whisper.utils": fake_utils},
        ):
            root = Path(temporary)
            resolved, was_cached = worker.ensure_model_reference(
                "large-v3-turbo", root, local_files_only=False,
            )
            self.assertEqual(Path(resolved), (root / "large-v3-turbo").resolve())
            self.assertFalse(was_cached)
            self.assertEqual(calls[0][0], "large-v3-turbo")
            self.assertEqual(Path(calls[0][1]), root / "large-v3-turbo")


if __name__ == "__main__":
    unittest.main()
