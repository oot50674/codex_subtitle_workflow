#!/usr/bin/env python3
"""Isolated faster-whisper worker used by subflow.py.

This file runs inside the project-local Whisper virtual environment.  It keeps
the heavyweight ML dependency out of the main subtitle workflow environment
and exchanges deterministic JSON artifacts with the parent CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_model_files(model_root: Path) -> list[str]:
    if not model_root.is_dir():
        return []
    found: set[str] = set()
    for model_file in model_root.rglob("model.bin"):
        found.add(str(model_file.parent.resolve()))
    return sorted(found)


def is_model_directory(path: Path) -> bool:
    return (path / "model.bin").is_file() and (path / "config.json").is_file()


def ensure_model_reference(
    model: str,
    model_root: Path,
    *,
    local_files_only: bool,
) -> tuple[str, bool]:
    """Return a local model path, downloading into the project cache if needed."""
    explicit = Path(model).expanduser()
    if explicit.is_dir() and is_model_directory(explicit):
        return str(explicit.resolve()), True

    cache_name = model.replace("/", "--").replace("\\", "--")
    candidate = model_root / cache_name
    if is_model_directory(candidate):
        return str(candidate.resolve()), True

    if local_files_only:
        raise FileNotFoundError(
            f"Model is not cached at {candidate}; remove --local-files-only "
            "to download it"
        )

    from faster_whisper.utils import download_model

    print(
        f"[subflow-whisper] model cache miss; downloading {model} to {candidate}",
        file=sys.stderr,
        flush=True,
    )
    downloaded = Path(download_model(
        model,
        output_dir=str(candidate),
        local_files_only=False,
        cache_dir=str(model_root / ".download-cache"),
    ))
    if not is_model_directory(downloaded):
        raise RuntimeError(f"Downloaded model is incomplete: {downloaded}")
    return str(downloaded.resolve()), False


def command_doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "python": sys.version.split()[0],
        "executable": str(Path(sys.executable).resolve()),
        "model_root": str(args.model_root.expanduser().resolve()),
        "cached_models": cached_model_files(args.model_root.expanduser().resolve()),
    }
    try:
        import ctranslate2
        import faster_whisper  # noqa: F401

        report.update({
            "ok": True,
            "faster_whisper": importlib.metadata.version("faster-whisper"),
            "ctranslate2": importlib.metadata.version("ctranslate2"),
            "cuda_device_count": ctranslate2.get_cuda_device_count(),
        })
        try:
            import onnxruntime
            from faster_whisper.utils import get_assets_path

            assets = Path(get_assets_path())
            encoder = assets / "silero_encoder_v5.onnx"
            decoder = assets / "silero_decoder_v5.onnx"
            vad_ready = encoder.is_file() and decoder.is_file()
            report["onnxruntime"] = onnxruntime.__version__
            report["silero_vad"] = {
                "available": vad_ready,
                "model": "silero-vad-v5-onnx",
                "encoder": str(encoder.resolve()),
                "decoder": str(decoder.resolve()),
                "error": None if vad_ready else "Bundled Silero VAD v5 ONNX assets are missing",
            }
        except Exception as vad_exc:
            report["silero_vad"] = {
                "available": False,
                "error": f"{type(vad_exc).__name__}: {vad_exc}",
            }
    except Exception as exc:  # diagnostic command must return structured detail
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def word_payload(word: Any) -> dict[str, Any]:
    return {
        "start_s": round(float(word.start), 3),
        "end_s": round(float(word.end), 3),
        "text": str(word.word),
        "probability": round(float(word.probability), 6),
    }


def segment_payload(segment: Any, *, include_words: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(segment.id),
        "seek": int(segment.seek),
        "start_s": round(float(segment.start), 3),
        "end_s": round(float(segment.end), 3),
        "text": str(segment.text).strip(),
        "temperature": float(segment.temperature),
        "avg_logprob": round(float(segment.avg_logprob), 6),
        "compression_ratio": round(float(segment.compression_ratio), 6),
        "no_speech_prob": round(float(segment.no_speech_prob), 6),
    }
    if include_words:
        payload["words"] = [word_payload(word) for word in (segment.words or [])]
    return payload


def command_transcribe(args: argparse.Namespace) -> int:
    from faster_whisper import WhisperModel

    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input media does not exist: {source}")
    model_root = args.model_root.expanduser().resolve()
    model_root.mkdir(parents=True, exist_ok=True)

    model_reference, model_was_cached = ensure_model_reference(
        args.model,
        model_root,
        local_files_only=args.local_files_only,
    )
    print(
        f"[subflow-whisper] loading model={args.model} device={args.device} "
        f"compute_type={args.compute_type}",
        file=sys.stderr,
        flush=True,
    )
    model = WhisperModel(
        model_reference,
        device=args.device,
        compute_type=args.compute_type,
        download_root=str(model_root),
        local_files_only=args.local_files_only,
    )
    segments, info = model.transcribe(
        str(source),
        language=args.language,
        beam_size=args.beam_size,
        vad_filter=args.vad_filter,
        word_timestamps=args.word_timestamps,
        condition_on_previous_text=not args.no_condition_on_previous_text,
        initial_prompt=args.initial_prompt,
        temperature=0.0,
    )

    items: list[dict[str, Any]] = []
    next_progress = 0.1
    duration = max(float(info.duration), 0.001)
    for segment in segments:
        item = segment_payload(segment, include_words=args.word_timestamps)
        if item["text"]:
            items.append(item)
        progress = min(float(segment.end) / duration, 1.0)
        if progress >= next_progress:
            print(
                f"[subflow-whisper] progress={progress:.0%}",
                file=sys.stderr,
                flush=True,
            )
            next_progress += 0.1

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "input": str(source),
        "model": args.model,
        "resolved_model": model_reference,
        "model_was_cached": model_was_cached,
        "model_root": str(model_root),
        "device": args.device,
        "compute_type": args.compute_type,
        "requested_language": args.language,
        "detected_language": info.language,
        "language_probability": round(float(info.language_probability), 6),
        "duration_s": round(float(info.duration), 3),
        "duration_after_vad_s": round(float(info.duration_after_vad), 3),
        "options": {
            "beam_size": args.beam_size,
            "vad_filter": args.vad_filter,
            "word_timestamps": args.word_timestamps,
            "condition_on_previous_text": not args.no_condition_on_previous_text,
            "initial_prompt": args.initial_prompt,
            "local_files_only": args.local_files_only,
        },
        "segments": items,
    }
    write_json_atomic(args.output_json, payload)
    print(str(args.output_json.expanduser().resolve()))
    return 0


def command_vad(args: argparse.Namespace) -> int:
    """Run the faster-whisper bundled Silero VAD without loading Whisper."""
    source = args.input.expanduser().resolve()
    output = args.output_json.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Input audio does not exist: {source}")
    if output == source:
        raise ValueError("VAD output JSON must not overwrite its input audio")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing VAD output: {output}")
    if not 0.0 < args.threshold < 1.0:
        raise ValueError("--threshold must be between 0 and 1")
    if args.neg_threshold is not None:
        if not 0.0 < args.neg_threshold < args.threshold:
            raise ValueError("--neg-threshold must be positive and lower than --threshold")
    if args.min_speech_ms < 0:
        raise ValueError("--min-speech-ms cannot be negative")
    if args.min_silence_ms < 0:
        raise ValueError("--min-silence-ms cannot be negative")
    if args.speech_pad_ms < 0:
        raise ValueError("--speech-pad-ms cannot be negative")
    if args.max_speech_seconds is not None and args.max_speech_seconds <= 0:
        raise ValueError("--max-speech-seconds must be positive")

    import numpy as np
    import onnxruntime
    from faster_whisper.audio import decode_audio
    from faster_whisper.utils import get_assets_path
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    assets = Path(get_assets_path())
    encoder = (assets / "silero_encoder_v5.onnx").resolve()
    decoder = (assets / "silero_decoder_v5.onnx").resolve()
    if not encoder.is_file() or not decoder.is_file():
        raise FileNotFoundError("Bundled Silero VAD v5 ONNX assets are missing")

    sampling_rate = 16000
    audio = decode_audio(str(source), sampling_rate=sampling_rate)
    if not isinstance(audio, np.ndarray) or audio.ndim != 1:
        raise RuntimeError("Silero VAD expected one mono audio channel")
    duration_ms = round(len(audio) * 1000 / sampling_rate)
    options = VadOptions(
        threshold=args.threshold,
        neg_threshold=args.neg_threshold,
        min_speech_duration_ms=args.min_speech_ms,
        max_speech_duration_s=(
            float("inf") if args.max_speech_seconds is None else args.max_speech_seconds
        ),
        min_silence_duration_ms=args.min_silence_ms,
        speech_pad_ms=args.speech_pad_ms,
    )
    timestamps = get_speech_timestamps(
        audio,
        vad_options=options,
        sampling_rate=sampling_rate,
    )
    intervals = [
        [
            round(int(item["start"]) * 1000 / sampling_rate),
            round(int(item["end"]) * 1000 / sampling_rate),
        ]
        for item in timestamps
    ]

    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "input": {
            "path": str(source),
            "sha256": sha256(source),
            "sampling_rate_hz": sampling_rate,
            "sample_count": len(audio),
            "duration_ms": duration_ms,
        },
        "detector": {
            "kind": "silero-vad",
            "implementation": "faster-whisper.vad",
            "model": "silero-vad-v5-onnx",
            "faster_whisper_version": importlib.metadata.version("faster-whisper"),
            "onnxruntime_version": onnxruntime.__version__,
            "numpy_version": np.__version__,
            "assets": {
                "encoder": {"path": str(encoder), "sha256": sha256(encoder)},
                "decoder": {"path": str(decoder), "sha256": sha256(decoder)},
            },
        },
        "parameters": {
            "threshold": args.threshold,
            "neg_threshold": args.neg_threshold,
            "min_speech_duration_ms": args.min_speech_ms,
            "min_silence_duration_ms": args.min_silence_ms,
            "speech_pad_ms": args.speech_pad_ms,
            "max_speech_duration_s": args.max_speech_seconds,
        },
        "speech_intervals_ms": intervals,
        "speech_interval_count": len(intervals),
        "speech_duration_ms": sum(end - start for start, end in intervals),
    }
    write_json_atomic(output, payload)
    print(str(output))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="subflow-whisper-worker")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--model-root", type=Path, required=True)
    doctor.set_defaults(func=command_doctor)

    transcribe = sub.add_parser("transcribe")
    transcribe.add_argument("--input", type=Path, required=True)
    transcribe.add_argument("--output-json", type=Path, required=True)
    transcribe.add_argument("--model-root", type=Path, required=True)
    transcribe.add_argument("--model", default="large-v3-turbo")
    transcribe.add_argument("--language")
    transcribe.add_argument("--device", default="cuda")
    transcribe.add_argument("--compute-type", default="float16")
    transcribe.add_argument("--beam-size", type=int, default=5)
    transcribe.add_argument("--vad-filter", action="store_true")
    transcribe.add_argument("--word-timestamps", action="store_true")
    transcribe.add_argument("--no-condition-on-previous-text", action="store_true")
    transcribe.add_argument("--initial-prompt")
    transcribe.add_argument("--local-files-only", action="store_true")
    transcribe.set_defaults(func=command_transcribe)

    vad = sub.add_parser("vad")
    vad.add_argument("--input", type=Path, required=True)
    vad.add_argument("--output-json", type=Path, required=True)
    vad.add_argument("--threshold", type=float, default=0.5)
    vad.add_argument("--neg-threshold", type=float)
    vad.add_argument("--min-speech-ms", type=int, default=100)
    vad.add_argument("--min-silence-ms", type=int, default=250)
    vad.add_argument("--speech-pad-ms", type=int, default=100)
    vad.add_argument("--max-speech-seconds", type=float)
    vad.set_defaults(func=command_vad)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
