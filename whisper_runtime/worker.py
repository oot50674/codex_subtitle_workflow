#!/usr/bin/env python3
"""Isolated faster-whisper worker used by subflow.py.

This file runs inside the project-local Whisper virtual environment.  It keeps
the heavyweight ML dependency out of the main subtitle workflow environment
and exchanges deterministic JSON artifacts with the parent CLI.
"""

from __future__ import annotations

import argparse
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
