#!/usr/bin/env python3
"""Agent-in-the-loop subtitle review workflow.

The CLI owns deterministic media/subtitle work.  A language-capable agent owns
correction and translation decisions written to decisions.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_FFMPEG_DOWNLOAD_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)
DEFAULT_FFMPEG_INSTALL_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    / "SubtitleWorkflow" / "ffmpeg"
)
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_WHISPER_RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "whisper"
DEFAULT_YOUTUBE_RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "youtube"
DEFAULT_WHISPER_MODEL_ROOT = Path(
    os.environ.get(
        "SUBFLOW_WHISPER_MODEL_ROOT",
        DEFAULT_WHISPER_RUNTIME_ROOT / "models",
    )
)
WHISPER_WORKER = PROJECT_ROOT / "whisper_runtime" / "worker.py"
WHISPER_REQUIREMENTS = PROJECT_ROOT / "whisper_runtime" / "requirements.txt"
YOUTUBE_REQUIREMENTS = PROJECT_ROOT / "youtube_runtime" / "requirements.txt"
TIME_RE = re.compile(
    r"^(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})"
    r"(?P<settings>.*)$"
)
TIME_POINT_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{1,2})"
    r"(?:[,.](?P<millis>\d{1,3}))?$"
)


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cue:
    index: int
    start_ms: int
    end_ms: int
    text: str
    settings: str = ""

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WorkflowError(f"Could not decode subtitle file: {path}")


def _ms(h: str, m: str, s: str, ms: str) -> int:
    return (((int(h) * 60) + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_srt(path: Path) -> list[Cue]:
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise WorkflowError(f"Subtitle file is empty: {path}")

    blocks = re.split(r"\n{2,}", text)
    cues: list[Cue] = []
    for block_no, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise WorkflowError(f"Malformed SRT block {block_no}: expected at least 3 lines")
        try:
            index = int(lines[0].strip())
        except ValueError as exc:
            raise WorkflowError(f"Malformed cue index in block {block_no}: {lines[0]!r}") from exc
        match = TIME_RE.match(lines[1].strip())
        if not match:
            raise WorkflowError(f"Malformed timestamp in cue {index}: {lines[1]!r}")
        g = match.groupdict()
        start = _ms(g["sh"], g["sm"], g["ss"], g["sms"])
        end = _ms(g["eh"], g["em"], g["es"], g["ems"])
        cue_text = "\n".join(lines[2:]).strip()
        if not cue_text:
            raise WorkflowError(f"Cue {index} has no text")
        cues.append(Cue(index, start, end, cue_text, g["settings"].strip()))

    seen: set[int] = set()
    for cue in cues:
        if cue.index in seen:
            raise WorkflowError(f"Duplicate cue index: {cue.index}")
        seen.add(cue.index)
    return cues


def format_timestamp(value_ms: int) -> str:
    if value_ms < 0:
        raise WorkflowError("Negative subtitle timestamp")
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(path: Path, cues: Iterable[Cue]) -> None:
    chunks = []
    for cue in cues:
        settings = f" {cue.settings}" if cue.settings else ""
        chunks.append(
            f"{cue.index}\n{format_timestamp(cue.start_ms)} --> "
            f"{format_timestamp(cue.end_ms)}{settings}\n{cue.text}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks), encoding="utf-8-sig", newline="\n")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def subtitle_sidecar_path(media: Path, language: str) -> Path:
    language = str(language).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", language):
        raise WorkflowError(f"Unsafe subtitle language suffix: {language!r}")
    media = media.expanduser().resolve()
    return media.with_name(f"{media.stem}.{language}.srt")


def copy_file_atomic(source: Path, destination: Path) -> None:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"Required source file is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_time_point(value: str | None) -> int | None:
    """Parse seconds or HH:MM:SS.mmm/MM:SS.mmm into milliseconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise WorkflowError("Time value cannot be empty")
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text):
        seconds = float(text.replace(",", "."))
        return round(seconds * 1000)
    match = TIME_POINT_RE.fullmatch(text)
    if not match:
        raise WorkflowError(
            f"Invalid time {value!r}; use seconds, MM:SS.mmm, or HH:MM:SS.mmm"
        )
    fields = match.groupdict()
    hours = int(fields["hours"] or 0)
    minutes = int(fields["minutes"])
    seconds = int(fields["seconds"])
    if minutes >= 60 and fields["hours"] is not None:
        raise WorkflowError(f"Invalid minutes in time {value!r}")
    if seconds >= 60:
        raise WorkflowError(f"Invalid seconds in time {value!r}")
    millis_text = (fields["millis"] or "0").ljust(3, "0")
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + int(millis_text)


def whisper_runtime_python(
    runtime_root: Path = DEFAULT_WHISPER_RUNTIME_ROOT,
    explicit: Path | None = None,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    environment = os.environ.get("SUBFLOW_WHISPER_PYTHON")
    if environment:
        return Path(environment).expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    if os.name == "nt":
        return runtime_root / "venv" / "Scripts" / "python.exe"
    return runtime_root / "venv" / "bin" / "python"


def install_whisper_runtime(runtime_root: Path, *, upgrade: bool = False) -> Path:
    runtime_root = runtime_root.expanduser().resolve()
    if not WHISPER_REQUIREMENTS.is_file() or not WHISPER_WORKER.is_file():
        raise WorkflowError("Whisper runtime support files are missing from the project")
    python = whisper_runtime_python(runtime_root)
    if not python.is_file():
        runtime_root.mkdir(parents=True, exist_ok=True)
        run([sys.executable, "-m", "venv", str(runtime_root / "venv")])
    command = [
        str(python), "-X", "utf8", "-m", "pip", "install",
        "--disable-pip-version-check",
    ]
    if upgrade:
        command.append("--upgrade")
    command.extend(["-r", str(WHISPER_REQUIREMENTS)])
    run(command)
    return python


def youtube_runtime_python(
    runtime_root: Path = DEFAULT_YOUTUBE_RUNTIME_ROOT,
    explicit: Path | None = None,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    environment = os.environ.get("SUBFLOW_YOUTUBE_PYTHON")
    if environment:
        return Path(environment).expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    if os.name == "nt":
        return runtime_root / "venv" / "Scripts" / "python.exe"
    return runtime_root / "venv" / "bin" / "python"


def install_youtube_runtime(runtime_root: Path, *, upgrade: bool = False) -> Path:
    runtime_root = runtime_root.expanduser().resolve()
    if not YOUTUBE_REQUIREMENTS.is_file():
        raise WorkflowError("YouTube runtime requirements are missing from the project")
    python = youtube_runtime_python(runtime_root)
    if not python.is_file():
        runtime_root.mkdir(parents=True, exist_ok=True)
        run([sys.executable, "-m", "venv", str(runtime_root / "venv")])
    command = [
        str(python), "-X", "utf8", "-m", "pip", "install",
        "--disable-pip-version-check",
    ]
    if upgrade:
        command.append("--upgrade")
    command.extend(["-r", str(YOUTUBE_REQUIREMENTS)])
    run(command)
    return python


def validate_youtube_url(value: str) -> str:
    parsed = urllib.parse.urlparse(str(value).strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
    if parsed.scheme != "https" or not allowed:
        raise WorkflowError("Expected an HTTPS youtube.com or youtu.be URL")
    return parsed.geturl()


def manifest_id_for(
    *, video_sha256: str, subtitle_sha256: str, source_language: str,
    target_language: str, cues: Iterable[Cue | dict[str, Any]],
) -> str:
    normalized_cues = []
    for cue in cues:
        if isinstance(cue, Cue):
            normalized_cues.append({
                "index": cue.index, "start_ms": cue.start_ms,
                "end_ms": cue.end_ms, "source": cue.text,
            })
        else:
            normalized_cues.append({
                "index": int(cue["index"]), "start_ms": int(cue["start_ms"]),
                "end_ms": int(cue["end_ms"]), "source": str(cue["source"]),
            })
    payload = {
        "schema_version": SCHEMA_VERSION,
        "video_sha256": video_sha256,
        "subtitle_sha256": subtitle_sha256,
        "source_language": source_language,
        "target_language": target_language,
        "cues": normalized_cues,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_manifest(path: Path, *, verify_sources: bool = True) -> dict[str, Any]:
    path = path.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(f"Unsupported manifest schema in {path}: {payload.get('schema_version')}")
    for key in ("manifest_id", "source_language", "target_language", "source", "cues", "summary"):
        if key not in payload:
            raise WorkflowError(f"Manifest is missing {key!r}: {path}")
    source = payload["source"]
    for key in ("video", "subtitle", "video_sha256", "subtitle_sha256"):
        if key not in source:
            raise WorkflowError(f"Manifest source is missing {key!r}: {path}")
    expected_id = manifest_id_for(
        video_sha256=str(source["video_sha256"]),
        subtitle_sha256=str(source["subtitle_sha256"]),
        source_language=str(payload["source_language"]),
        target_language=str(payload["target_language"]),
        cues=payload["cues"],
    )
    if payload["manifest_id"] != expected_id:
        raise WorkflowError(f"Manifest fingerprint mismatch: {path}")
    if not verify_sources:
        return payload

    video = Path(source["video"])
    subtitle = Path(source["subtitle"])
    if not video.is_file() or not subtitle.is_file():
        raise WorkflowError("A manifest source file no longer exists")
    if sha256(video) != source["video_sha256"]:
        raise WorkflowError(f"Video changed after prepare: {video}")
    if sha256(subtitle) != source["subtitle_sha256"]:
        raise WorkflowError(f"Subtitle changed after prepare: {subtitle}")
    actual_cues = parse_srt(subtitle)
    expected_cues = payload["cues"]
    if len(actual_cues) != len(expected_cues):
        raise WorkflowError("Manifest cue count no longer matches the source subtitle")
    for actual, expected in zip(actual_cues, expected_cues):
        if (
            actual.index != int(expected["index"])
            or actual.start_ms != int(expected["start_ms"])
            or actual.end_ms != int(expected["end_ms"])
            or actual.text != str(expected["source"])
        ):
            raise WorkflowError(f"Manifest/source mismatch at cue {actual.index}")
    return payload


def _tools_under_root(root: Path) -> tuple[Path, Path] | None:
    for directory in (root / "bin", root):
        ffmpeg = directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        ffprobe = directory / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg.resolve(), ffprobe.resolve()
    return None


def tool_paths(ffmpeg_root: Path | None = None) -> tuple[Path, Path]:
    """Find FFmpeg from an explicit root, environment variables, or PATH."""
    roots: list[Path] = []
    if ffmpeg_root is not None:
        roots.append(Path(ffmpeg_root).expanduser())
    for variable in ("SUBFLOW_FFMPEG_ROOT", "FFMPEG_ROOT"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value).expanduser())
    for root in roots:
        pair = _tools_under_root(root)
        if pair:
            return pair

    ffmpeg_on_path = shutil.which("ffmpeg")
    ffprobe_on_path = shutil.which("ffprobe")
    if ffmpeg_on_path and ffprobe_on_path:
        return Path(ffmpeg_on_path).resolve(), Path(ffprobe_on_path).resolve()

    pair = _tools_under_root(DEFAULT_FFMPEG_INSTALL_ROOT)
    if pair:
        return pair
    raise WorkflowError(
        "FFmpeg was not found in PATH, SUBFLOW_FFMPEG_ROOT, or FFMPEG_ROOT. "
        "Run 'subflow doctor --install-ffmpeg' to install a local copy."
    )


def _download(url: str, destination: Path, *, maximum_bytes: int) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "file"}:
        raise WorkflowError(f"Refusing non-HTTPS download URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "SubtitleWorkflow/1"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise WorkflowError(f"Download exceeded the safety limit: {url}")
            handle.write(chunk)


def _remote_sha256(url: str, destination: Path) -> str:
    checksum_file = destination.with_suffix(destination.suffix + ".sha256")
    _download(url, checksum_file, maximum_bytes=64 * 1024)
    match = re.search(r"\b[0-9a-fA-F]{64}\b", checksum_file.read_text(encoding="utf-8"))
    if not match:
        raise WorkflowError(f"Could not parse SHA-256 checksum from {url}")
    return match.group(0).lower()


def _add_to_user_path(directory: Path) -> bool:
    directory_text = str(directory.resolve())
    current_entries = [item for item in os.environ.get("PATH", "").split(os.pathsep) if item]
    if directory_text.casefold() not in {item.casefold() for item in current_entries}:
        os.environ["PATH"] = os.pathsep.join([directory_text, *current_entries])
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                user_path, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                user_path = ""
            entries = [item for item in str(user_path).split(";") if item]
            if directory_text.casefold() not in {item.casefold() for item in entries}:
                updated = ";".join([*entries, directory_text])
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)
        return True
    except OSError:
        return False


def install_ffmpeg(
    install_root: Path = DEFAULT_FFMPEG_INSTALL_ROOT,
    *,
    download_url: str = DEFAULT_FFMPEG_DOWNLOAD_URL,
    expected_sha256: str | None = None,
    skip_checksum: bool = False,
    persist_path: bool = True,
) -> tuple[Path, Path, bool]:
    """Download, verify, and install a Windows FFmpeg essentials ZIP."""
    if os.name != "nt":
        raise WorkflowError("Automatic FFmpeg installation is currently supported on Windows only")
    install_root = install_root.expanduser().resolve()
    existing = _tools_under_root(install_root)
    if existing:
        return existing[0], existing[1], False
    if install_root.exists() and any(install_root.iterdir()):
        raise WorkflowError(f"Refusing to overwrite a non-empty FFmpeg directory: {install_root}")

    with tempfile.TemporaryDirectory(prefix="subflow-ffmpeg-") as temporary:
        temp_root = Path(temporary)
        archive_path = temp_root / "ffmpeg.zip"
        print(f"Downloading FFmpeg from {download_url}")
        _download(download_url, archive_path, maximum_bytes=1024 * 1024 * 1024)
        if not skip_checksum:
            expected = (expected_sha256 or _remote_sha256(download_url + ".sha256", archive_path)).lower()
            actual = sha256(archive_path).lower()
            if actual != expected:
                raise WorkflowError(f"FFmpeg checksum mismatch: expected {expected}, got {actual}")

        extract_root = temp_root / "extracted"
        extract_root.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                base = extract_root.resolve()
                for member in archive.infolist():
                    target = (base / member.filename).resolve()
                    if target != base and base not in target.parents:
                        raise WorkflowError(f"Unsafe path in FFmpeg archive: {member.filename}")
                archive.extractall(extract_root)
        except zipfile.BadZipFile as exc:
            raise WorkflowError("Downloaded FFmpeg archive is not a valid ZIP file") from exc

        candidates = []
        for ffmpeg in extract_root.rglob("ffmpeg.exe"):
            if ffmpeg.parent.name.casefold() != "bin":
                continue
            ffprobe = ffmpeg.parent / "ffprobe.exe"
            if ffprobe.is_file():
                candidates.append(ffmpeg.parent.parent)
        if len(candidates) != 1:
            raise WorkflowError(f"Expected one FFmpeg build in the archive, found {len(candidates)}")

        if install_root.exists():
            install_root.rmdir()
        install_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidates[0], install_root)

    pair = _tools_under_root(install_root)
    if not pair:
        raise WorkflowError(f"FFmpeg installation validation failed: {install_root}")
    path_persisted = _add_to_user_path(pair[0].parent) if persist_path else False
    return pair[0], pair[1], path_persisted


def run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def probe_video(ffprobe: Path, video: Path) -> dict[str, Any]:
    completed = run(
        [
            str(ffprobe), "-v", "error", "-show_entries",
            "format=filename,duration,size,bit_rate:stream=index,codec_type,codec_name,"
            "width,height,r_frame_rate,sample_rate,channels,channel_layout",
            "-of", "json", str(video),
        ],
        capture=True,
    )
    return json.loads(completed.stdout)


def cue_flags(cue: Cue, previous: Cue | None) -> list[str]:
    flags: list[str] = []
    compact = re.sub(r"\s+", " ", cue.text).strip()
    duration_s = max(cue.duration_ms / 1000, 0.001)
    cps = len(compact) / duration_s
    if cue.duration_ms <= 0:
        flags.append("invalid_duration")
    elif cue.duration_ms < 800:
        flags.append("very_short_duration")
    if cps > 25:
        flags.append("high_reading_speed")
    if len(compact) > 100:
        flags.append("long_text")
    if previous and cue.start_ms < previous.end_ms:
        flags.append("overlap")
    if re.search(r"\b(\w+)(?:\s+\1){2,}\b", compact, re.IGNORECASE):
        flags.append("repeated_word")
    if "�" in compact:
        flags.append("encoding_replacement")
    return flags


def split_lines(text: str, max_chars: int, max_lines: int | None = None) -> str:
    """Balance wrapped lines, optionally enforcing a hard line-count limit."""
    if not text.strip() or max_chars <= 0:
        return text.strip()
    if max_lines is not None:
        if max_lines <= 0:
            raise WorkflowError("max_lines must be positive")
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= max_chars or max_lines == 1:
            return compact
        if max_lines == 2:
            midpoint = len(compact) / 2
            candidates = [match.start() for match in re.finditer(r"\s", compact)]
            if not candidates:
                boundary = round(midpoint)
                return f"{compact[:boundary]}\n{compact[boundary:]}"
            boundary = min(candidates, key=lambda value: abs(value - midpoint))
            return f"{compact[:boundary].rstrip()}\n{compact[boundary:].lstrip()}"
    output: list[str] = []
    for paragraph in text.splitlines():
        if not paragraph:
            output.append("")
            continue
        remaining = paragraph
        while len(remaining) > max_chars:
            lines_needed = max(2, (len(remaining) + max_chars - 1) // max_chars)
            ideal = round(len(remaining) / lines_needed)
            candidates = [
                match.start() for match in re.finditer(r"\s", remaining)
                if 1 <= match.start() <= max_chars
            ]
            boundary = min(candidates, key=lambda value: abs(value - ideal)) if candidates else max_chars
            output.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
        output.append(remaining)
    return "\n".join(output)


def cue_to_manifest(cue: Cue, previous: Cue | None) -> dict[str, Any]:
    compact = re.sub(r"\s+", " ", cue.text).strip()
    duration_s = max(cue.duration_ms / 1000, 0.001)
    return {
        "index": cue.index,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "duration_ms": cue.duration_ms,
        "source": cue.text,
        "characters_per_second": round(len(compact) / duration_s, 2),
        "gap_from_previous_ms": None if previous is None else cue.start_ms - previous.end_ms,
        "flags": cue_flags(cue, previous),
    }


def command_doctor(args: argparse.Namespace) -> int:
    installed = False
    path_persisted = False
    try:
        ffmpeg, ffprobe = tool_paths(args.ffmpeg_root)
    except WorkflowError:
        if not args.install_ffmpeg:
            raise
        install_root = args.ffmpeg_root or DEFAULT_FFMPEG_INSTALL_ROOT
        ffmpeg, ffprobe, path_persisted = install_ffmpeg(
            install_root,
            download_url=args.ffmpeg_download_url,
            expected_sha256=args.ffmpeg_sha256,
            skip_checksum=args.skip_download_checksum,
        )
        installed = True
    versions: dict[str, str] = {}
    for name, tool in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        result = run([str(tool), "-version"], capture=True)
        versions[name] = result.stdout.splitlines()[0]
    report = {
        "ok": True,
        "python": sys.version.split()[0],
        "installed_now": installed,
        "user_path_updated": path_persisted,
        "paths": {"ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe)},
        "executables": versions,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_whisper_doctor(args: argparse.Namespace) -> int:
    runtime_root = args.runtime_root.expanduser().resolve()
    model_root = args.model_root.expanduser().resolve()
    if args.upgrade_runtime and not args.install_runtime:
        raise WorkflowError("--upgrade-runtime requires --install-runtime")
    if args.install_runtime:
        if args.runtime_python is not None:
            raise WorkflowError("--install-runtime cannot be combined with --runtime-python")
        python = install_whisper_runtime(runtime_root, upgrade=args.upgrade_runtime)
    else:
        python = whisper_runtime_python(runtime_root, args.runtime_python)
    if not python.is_file():
        report = {
            "ok": False,
            "runtime_root": str(runtime_root),
            "python": str(python),
            "model_root": str(model_root),
            "error": "Whisper runtime is not installed",
            "hint": "Run 'python subflow.py whisper-doctor --install-runtime'",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    model_root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            str(python), "-X", "utf8", str(WHISPER_WORKER), "doctor",
            "--model-root", str(model_root),
        ],
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return 0 if completed.returncode == 0 else 2


def command_youtube_doctor(args: argparse.Namespace) -> int:
    runtime_root = args.runtime_root.expanduser().resolve()
    if args.upgrade_runtime and not args.install_runtime:
        raise WorkflowError("--upgrade-runtime requires --install-runtime")
    if args.install_runtime:
        if args.runtime_python is not None:
            raise WorkflowError("--install-runtime cannot be combined with --runtime-python")
        python = install_youtube_runtime(runtime_root, upgrade=args.upgrade_runtime)
    else:
        python = youtube_runtime_python(runtime_root, args.runtime_python)
    if not python.is_file():
        report = {
            "ok": False,
            "runtime_root": str(runtime_root),
            "python": str(python),
            "error": "YouTube download runtime is not installed",
            "hint": "Run 'python subflow.py youtube-doctor --install-runtime'",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    completed = run([str(python), "-X", "utf8", "-m", "yt_dlp", "--version"], capture=True)
    report = {
        "ok": True,
        "runtime_root": str(runtime_root),
        "python": str(python),
        "yt_dlp": completed.stdout.strip(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def command_download_youtube(args: argparse.Namespace) -> int:
    url = validate_youtube_url(args.url)
    runtime_root = args.runtime_root.expanduser().resolve()
    python = youtube_runtime_python(runtime_root, args.runtime_python)
    if not python.is_file():
        raise WorkflowError(
            f"YouTube download runtime is not installed: {python}. "
            "Run 'python subflow.py youtube-doctor --install-runtime'."
        )
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise WorkflowError(f"Output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg, _ = tool_paths(args.ffmpeg_root)
    command = [
        str(python), "-X", "utf8", "-m", "yt_dlp",
        "--ffmpeg-location", str(ffmpeg.parent),
        "--no-write-subs",
        "--no-write-auto-subs",
        "--paths", str(output_dir),
        "--output", args.output_template,
        "--print", "after_move:filepath",
    ]
    if not args.playlist:
        command.append("--no-playlist")
    if args.audio_only:
        command.extend(["--extract-audio", "--audio-format", args.audio_format])
    else:
        command.extend(["--format", args.format, "--merge-output-format", args.container])
    if args.force:
        command.append("--force-overwrites")
    command.append(url)
    completed = run(command, capture=True)
    paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    print(json.dumps({"ok": True, "url": url, "files": paths}, ensure_ascii=False, indent=2))
    return 0


def _transcription_metadata_path(output: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return output.with_name(f"{output.stem}.transcription.json")


def _whisper_worker_command(
    args: argparse.Namespace,
    *,
    python: Path,
    audio: Path,
    raw_output: Path,
    model_root: Path,
) -> list[str]:
    command = [
        str(python), "-X", "utf8", str(WHISPER_WORKER), "transcribe",
        "--input", str(audio),
        "--output-json", str(raw_output),
        "--model-root", str(model_root),
        "--model", args.model,
        "--device", args.device,
        "--compute-type", args.compute_type,
        "--beam-size", str(args.beam_size),
    ]
    if args.language:
        command.extend(["--language", args.language])
    if args.vad_filter:
        command.append("--vad-filter")
    if args.word_timestamps:
        command.append("--word-timestamps")
    if args.no_condition_on_previous_text:
        command.append("--no-condition-on-previous-text")
    if args.initial_prompt:
        command.extend(["--initial-prompt", args.initial_prompt])
    if args.local_files_only:
        command.append("--local-files-only")
    return command


def _transcribe_media(
    args: argparse.Namespace,
    *,
    media: Path,
    start_ms: int | None,
    end_ms: int | None,
    selection: dict[str, Any] | None = None,
) -> int:
    media = media.expanduser().resolve()
    if not media.is_file():
        raise WorkflowError(f"Input media does not exist: {media}")
    output = args.output.expanduser().resolve()
    metadata = _transcription_metadata_path(output, args.metadata)
    runtime_root = args.runtime_root.expanduser().resolve()
    python = whisper_runtime_python(runtime_root, args.runtime_python)
    if not python.is_file():
        raise WorkflowError(
            f"Whisper runtime is not installed: {python}. "
            "Run 'python subflow.py whisper-doctor --install-runtime'."
        )
    if not WHISPER_WORKER.is_file():
        raise WorkflowError(f"Whisper worker is missing: {WHISPER_WORKER}")
    model_root = args.model_root.expanduser().resolve()
    model_root.mkdir(parents=True, exist_ok=True)
    if args.beam_size <= 0:
        raise WorkflowError("--beam-size must be positive")
    ffmpeg, ffprobe = tool_paths(args.ffmpeg_root)
    probe = probe_video(ffprobe, media)
    try:
        duration_ms = round(float(probe["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError(f"Could not determine media duration: {media}") from exc
    if duration_ms <= 0:
        raise WorkflowError(f"Media duration is not positive: {media}")
    range_start = 0 if start_ms is None else start_ms
    range_end = duration_ms if end_ms is None else end_ms
    if range_start < 0:
        raise WorkflowError("Transcription start cannot be negative")
    if range_start >= duration_ms:
        raise WorkflowError("Transcription start is outside the media duration")
    if range_end > duration_ms:
        if range_end - duration_ms <= 250:
            range_end = duration_ms
        else:
            raise WorkflowError("Transcription end is outside the media duration")
    if range_end <= range_start:
        raise WorkflowError("Transcription end must be later than start")

    kept_audio = output.with_name(f"{output.stem}.source.wav") if args.keep_audio else None
    targets = [output, metadata, *([kept_audio] if kept_audio is not None else [])]
    if len({str(path).casefold() for path in targets}) != len(targets):
        raise WorkflowError("SRT, metadata, and preserved audio paths must be different")
    existing = [path for path in targets if path.exists()]
    if existing and not args.force:
        raise WorkflowError(f"Refusing to overwrite existing output: {existing[0]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="subflow-whisper-") as temporary:
        temp_root = Path(temporary)
        audio = temp_root / "source-16k-mono.wav"
        raw_output = temp_root / "worker-result.json"
        extraction = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(media),
        ]
        if range_start:
            extraction.extend(["-ss", f"{range_start / 1000:.3f}"])
        if range_start or range_end < duration_ms:
            extraction.extend(["-t", f"{(range_end - range_start) / 1000:.3f}"])
        extraction.extend([
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(audio),
        ])
        run(extraction)
        run(_whisper_worker_command(
            args,
            python=python,
            audio=audio,
            raw_output=raw_output,
            model_root=model_root,
        ))
        worker = json.loads(raw_output.read_text(encoding="utf-8"))
        if worker.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError("Unsupported Whisper worker result schema")
        raw_segments = worker.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise WorkflowError("Whisper produced no speech segments")

        cues: list[Cue] = []
        absolute_segments: list[dict[str, Any]] = []
        for index, item in enumerate(raw_segments, 1):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            segment_start = max(range_start, range_start + round(float(item["start_s"]) * 1000))
            segment_end = min(range_end, range_start + round(float(item["end_s"]) * 1000))
            if segment_end <= segment_start:
                segment_end = min(range_end, segment_start + 1)
            if segment_end <= segment_start:
                continue
            cues.append(Cue(len(cues) + 1, segment_start, segment_end, text))
            absolute = dict(item)
            absolute.update({
                "index": len(cues),
                "start_ms": segment_start,
                "end_ms": segment_end,
            })
            absolute.pop("start_s", None)
            absolute.pop("end_s", None)
            if isinstance(absolute.get("words"), list):
                adjusted_words = []
                for word in absolute["words"]:
                    adjusted = dict(word)
                    adjusted["start_ms"] = range_start + round(float(word["start_s"]) * 1000)
                    adjusted["end_ms"] = range_start + round(float(word["end_s"]) * 1000)
                    adjusted.pop("start_s", None)
                    adjusted.pop("end_s", None)
                    adjusted_words.append(adjusted)
                absolute["words"] = adjusted_words
            absolute_segments.append(absolute)
        if not cues:
            raise WorkflowError("Whisper produced no usable subtitle cues")

        source_sha256 = sha256(media)
        identity = {
            "source_sha256": source_sha256,
            "range_start_ms": range_start,
            "range_end_ms": range_end,
            "model": args.model,
            "language": args.language,
            "device": args.device,
            "compute_type": args.compute_type,
            "beam_size": args.beam_size,
            "vad_filter": args.vad_filter,
            "word_timestamps": args.word_timestamps,
        }
        transcription_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if kept_audio is not None:
            kept_audio.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audio, kept_audio)
        staged_srt = output.with_name(f".{output.name}.tmp-{os.getpid()}")
        try:
            write_srt(staged_srt, cues)
            staged_srt.replace(output)
        finally:
            staged_srt.unlink(missing_ok=True)

        metadata_payload = {
            "schema_version": SCHEMA_VERSION,
            "transcription_id": transcription_id,
            "created_at": utc_now(),
            "mode": "range" if (range_start or range_end < duration_ms) else "full",
            "source": {
                "media": str(media),
                "sha256": source_sha256,
                "duration_ms": duration_ms,
            },
            "range": {
                "start_ms": range_start,
                "end_ms": range_end,
                "duration_ms": range_end - range_start,
            },
            "selection": selection,
            "runtime": {
                "python": str(python),
                "runtime_root": str(runtime_root),
                "worker": str(WHISPER_WORKER),
                "model_root": str(model_root),
                "ffmpeg": str(ffmpeg),
                "ffprobe": str(ffprobe),
            },
            "model": {
                "name": args.model,
                "resolved_path": worker.get("resolved_model"),
                "was_cached": worker.get("model_was_cached"),
                "device": args.device,
                "compute_type": args.compute_type,
                "requested_language": args.language,
                "detected_language": worker.get("detected_language"),
                "language_probability": worker.get("language_probability"),
            },
            "options": worker.get("options", {}),
            "cue_count": len(cues),
            "segments": absolute_segments,
            "artifacts": {
                "srt": str(output),
                "metadata": str(metadata),
                "source_audio": None if kept_audio is None else str(kept_audio),
            },
        }
        write_json_atomic(metadata, metadata_payload)

    print(json.dumps({
        "ok": True,
        "transcription_id": transcription_id,
        "cue_count": len(cues),
        "srt": str(output),
        "metadata": str(metadata),
        "range_start_ms": range_start,
        "range_end_ms": range_end,
    }, ensure_ascii=False, indent=2))
    return 0


def command_transcribe(args: argparse.Namespace) -> int:
    return _transcribe_media(
        args,
        media=args.media,
        start_ms=parse_time_point(args.start),
        end_ms=parse_time_point(args.end),
    )


def command_transcribe_cues(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    cue_map = {int(item["index"]): item for item in manifest["cues"]}
    indexes = parse_cue_selection(args.cues, set(cue_map))
    if args.padding < 0:
        raise WorkflowError("--padding cannot be negative")
    padding_ms = round(args.padding * 1000)
    start_ms = max(0, min(int(cue_map[index]["start_ms"]) for index in indexes) - padding_ms)
    end_ms = max(int(cue_map[index]["end_ms"]) for index in indexes) + padding_ms
    selection = {
        "manifest": str(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "cues": indexes,
        "padding_ms": padding_ms,
    }
    return _transcribe_media(
        args,
        media=Path(manifest["source"]["video"]),
        start_ms=start_ms,
        end_ms=end_ms,
        selection=selection,
    )


def command_prepare(args: argparse.Namespace) -> int:
    video = args.video.resolve()
    subtitle = args.subtitle.resolve()
    workdir = args.workdir.resolve()
    if not video.is_file():
        raise WorkflowError(f"Video does not exist: {video}")
    if not subtitle.is_file():
        raise WorkflowError(f"Subtitle does not exist: {subtitle}")
    if video.is_relative_to(workdir) or subtitle.is_relative_to(workdir):
        raise WorkflowError("Workdir must not contain either source file")
    if workdir.exists() and any(workdir.iterdir()):
        if not args.force:
            raise WorkflowError(f"Workdir is not empty; choose a new directory or pass --force: {workdir}")
        if not (workdir / ".subflow-workdir").is_file():
            raise WorkflowError(f"Refusing --force because the workdir marker is missing: {workdir}")

    ffmpeg, ffprobe = tool_paths(args.ffmpeg_root)
    cues = parse_srt(subtitle)
    media = probe_video(ffprobe, video)
    video_hash = sha256(video)
    subtitle_hash = sha256(subtitle)
    manifest_cues = []
    previous = None
    for cue in cues:
        manifest_cues.append(cue_to_manifest(cue, previous))
        previous = cue
    manifest_id = manifest_id_for(
        video_sha256=video_hash,
        subtitle_sha256=subtitle_hash,
        source_language=args.source_language,
        target_language=args.target_language,
        cues=cues,
    )

    workdir.parent.mkdir(parents=True, exist_ok=True)
    staging = workdir.parent / f".{workdir.name}.staging-{os.getpid()}"
    if staging.exists():
        raise WorkflowError(f"Staging directory already exists: {staging}")
    staging.mkdir()
    backup: Path | None = None
    try:
        staged_audio = staging / "media" / "audio_16k_mono.wav"
        final_audio = workdir / "media" / "audio_16k_mono.wav"
        if not args.no_audio:
            staged_audio.parent.mkdir(parents=True, exist_ok=True)
            run([
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(staged_audio),
            ])

        staged_frames = staging / "media" / "frames"
        final_frames = workdir / "media" / "frames"
        frame_names: list[str] = []
        if not args.no_frames:
            staged_frames.mkdir(parents=True, exist_ok=True)
            run([
                str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                "-vf", f"fps=1/{args.frame_interval:g},scale=640:-2", "-q:v", "3",
                str(staged_frames / "%06d.jpg"),
            ])
            frame_names = [item.name for item in sorted(staged_frames.glob("*.jpg"))]

        manifest_path = workdir / "manifest.json"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "manifest_id": manifest_id,
            "created_at": utc_now(),
            "source_language": args.source_language,
            "target_language": args.target_language,
            "source": {
                "video": str(video),
                "subtitle": str(subtitle),
                "video_sha256": video_hash,
                "subtitle_sha256": subtitle_hash,
            },
            "tools": {
                "ffmpeg": str(ffmpeg),
                "ffprobe": str(ffprobe),
            },
            "media": media,
            "artifacts": {
                "audio": str(final_audio) if not args.no_audio else None,
                "audio_sha256": sha256(staged_audio) if not args.no_audio else None,
                "frames_dir": str(final_frames) if not args.no_frames else None,
                "frames": [str(final_frames / name) for name in frame_names],
                "frame_interval_seconds": None if args.no_frames else args.frame_interval,
            },
            "summary": {
                "cue_count": len(cues),
                "flagged_cue_count": sum(bool(item["flags"]) for item in manifest_cues),
            },
            "cues": manifest_cues,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        decisions = {
            "schema_version": SCHEMA_VERSION,
            "manifest": str(manifest_path),
            "manifest_id": manifest_id,
            "source_language": args.source_language,
            "target_language": args.target_language,
            "reference_docs": [],
            "translation_style": {
                "register": "natural tutorial narration",
                "preserve_technical_terms": True,
                "max_korean_line_chars": 24,
                "preferred_max_lines": 2,
                "overflow_policy": "condense first; split only with timing evidence",
            },
            "decisions": [
                {
                    "index": cue.index,
                    "source": cue.text,
                    "corrected": cue.text,
                    "translation": "",
                    "confidence": "unreviewed",
                    "notes": "",
                }
                for cue in cues
            ],
        }
        (staging / "decisions.template.json").write_text(
            json.dumps(decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / ".subflow-workdir").write_text(
            json.dumps({"schema_version": SCHEMA_VERSION, "manifest_id": manifest_id}) + "\n",
            encoding="utf-8",
        )

        if workdir.exists():
            if any(workdir.iterdir()):
                backup = workdir.parent / f".{workdir.name}.backup-{os.getpid()}"
                if backup.exists():
                    raise WorkflowError(f"Backup directory already exists: {backup}")
                workdir.rename(backup)
            else:
                workdir.rmdir()
        staging.rename(workdir)
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists() and not workdir.exists():
            backup.rename(workdir)
        raise

    manifest_path = workdir / "manifest.json"
    template_path = workdir / "decisions.template.json"
    print(f"Prepared {len(cues)} cues in {workdir}")
    print(f"Manifest: {manifest_path}")
    print(f"Decision template: {template_path}")
    return 0


def parse_cue_selection(selection: str, valid: set[int]) -> list[int]:
    selected: set[int] = set()
    for token in selection.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(token))
    unknown = selected - valid
    if unknown:
        raise WorkflowError(f"Unknown cue indexes: {sorted(unknown)}")
    return sorted(selected)


def command_evidence(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    cue_map = {int(item["index"]): item for item in manifest["cues"]}
    selection = parse_cue_selection(args.cues, set(cue_map))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    video = Path(manifest["source"]["video"])
    ffmpeg = Path(manifest["tools"]["ffmpeg"])
    packet_items = []
    ordered = manifest["cues"]
    position = {int(item["index"]): idx for idx, item in enumerate(ordered)}

    for index in selection:
        cue = cue_map[index]
        start = max(0.0, cue["start_ms"] / 1000 - args.padding)
        end = cue["end_ms"] / 1000 + args.padding
        duration = max(0.1, end - start)
        midpoint = (cue["start_ms"] + cue["end_ms"]) / 2000
        stem = f"cue_{index:04d}"
        audio = output / f"{stem}.wav"
        frame = output / f"{stem}.jpg"
        video_clip = output / f"{stem}.mp4"
        run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
            "-i", str(video), "-t", f"{duration:.3f}", "-map", "0:a:0", "-vn", "-ac", "1",
            "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
        ])
        run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{midpoint:.3f}",
            "-i", str(video), "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "2", str(frame),
        ])
        run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{duration:.3f}",
            "-map", "0:v:0", "-map", "0:a:0", "-vf", "scale=960:-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(video_clip),
        ])
        pos = position[index]
        context = ordered[max(0, pos - args.context): min(len(ordered), pos + args.context + 1)]
        packet_items.append({
            "index": index,
            "source": cue["source"],
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "flags": cue["flags"],
            "audio": str(audio),
            "frame": str(frame),
            "video_clip": str(video_clip),
            "context": [{"index": item["index"], "source": item["source"]} for item in context],
        })

    packet = {
        "schema_version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "created_at": utc_now(),
        "items": packet_items,
    }
    packet_path = output / "packet.json"
    packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence for {len(packet_items)} cues: {packet_path}")
    return 0


def normalize_intervals_ms(
    intervals: Iterable[Iterable[int | float] | dict[str, Any]],
    *,
    duration_ms: int,
    join_gap_ms: int = 0,
) -> list[tuple[int, int]]:
    """Validate, clip, sort, and union millisecond intervals deterministically."""
    if duration_ms <= 0:
        raise WorkflowError("Media duration must be positive")
    if join_gap_ms < 0:
        raise WorkflowError("Interval join gap cannot be negative")
    normalized: list[tuple[int, int]] = []
    for position, item in enumerate(intervals, 1):
        if isinstance(item, dict):
            raw_start = item.get("start_ms")
            raw_end = item.get("end_ms")
        else:
            if isinstance(item, (str, bytes)):
                raise WorkflowError(f"Interval {position} must not be text")
            values = list(item)
            if len(values) != 2:
                raise WorkflowError(f"Interval {position} must contain exactly two values")
            raw_start, raw_end = values
        try:
            start_value = float(raw_start)
            end_value = float(raw_end)
            if not math.isfinite(start_value) or not math.isfinite(end_value):
                raise ValueError("non-finite boundary")
            start = round(start_value)
            end = round(end_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkflowError(f"Interval {position} contains a non-numeric boundary") from exc
        if end <= start:
            raise WorkflowError(f"Interval {position} has a non-positive duration")
        start = max(0, min(duration_ms, start))
        end = max(0, min(duration_ms, end))
        if end > start:
            normalized.append((start, end))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(normalized):
        if merged and start <= merged[-1][1] + join_gap_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def complement_intervals_ms(
    intervals: Iterable[Iterable[int | float] | dict[str, Any]],
    *,
    duration_ms: int,
) -> list[tuple[int, int]]:
    normalized = normalize_intervals_ms(intervals, duration_ms=duration_ms)
    complement: list[tuple[int, int]] = []
    cursor = 0
    for start, end in normalized:
        if start > cursor:
            complement.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_ms:
        complement.append((cursor, duration_ms))
    return complement


def _range_intersections(
    start_ms: int,
    end_ms: int,
    intervals: list[tuple[int, int]],
) -> list[tuple[int, int, int]]:
    matches: list[tuple[int, int, int]] = []
    for interval_index, (start, end) in enumerate(intervals):
        overlap_start = max(start_ms, start)
        overlap_end = min(end_ms, end)
        if overlap_end > overlap_start:
            matches.append((interval_index, overlap_start, overlap_end))
    return matches


def _subtract_covered_range(
    start_ms: int,
    end_ms: int,
    coverage: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    if end_ms <= start_ms:
        return []
    uncovered: list[tuple[int, int]] = []
    cursor = start_ms
    for cover_start, cover_end in coverage:
        if cover_end <= cursor:
            continue
        if cover_start >= end_ms:
            break
        if cover_start > cursor:
            uncovered.append((cursor, min(cover_start, end_ms)))
        cursor = max(cursor, min(cover_end, end_ms))
        if cursor >= end_ms:
            break
    if cursor < end_ms:
        uncovered.append((cursor, end_ms))
    return [(start, end) for start, end in uncovered if end > start]


def _speech_not_covered_by_subtitles(
    start_ms: int,
    end_ms: int,
    speech: list[tuple[int, int]],
    subtitle_coverage: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    uncovered: list[tuple[int, int]] = []
    for _, overlap_start, overlap_end in _range_intersections(start_ms, end_ms, speech):
        uncovered.extend(_subtract_covered_range(overlap_start, overlap_end, subtitle_coverage))
    return uncovered


def _nearest_boundary(boundaries: Iterable[int], point_ms: int, window_ms: int) -> int | None:
    candidates = [value for value in boundaries if abs(value - point_ms) <= window_ms]
    if not candidates:
        return None
    return min(candidates, key=lambda value: (abs(value - point_ms), value))


def _timing_weight(value_ms: int, threshold_ms: int, *, base: float, maximum: float) -> float:
    excess_ratio = max(0.0, value_ms / max(threshold_ms, 1) - 1.0)
    return round(min(maximum, base + excess_ratio * 0.12), 4)


def _candidate_score(reasons: list[dict[str, Any]]) -> float:
    if not reasons:
        return 0.0
    families = {
        "no_detected_speech_overlap": "overlap",
        "low_speech_overlap": "overlap",
        "subtitle_starts_before_speech": "contained_silence",
        "subtitle_ends_after_speech": "contained_silence",
        "long_internal_silence": "contained_silence",
        "speech_without_subtitle": "uncovered_speech",
    }
    family_weights: dict[str, float] = {}
    for reason in reasons:
        family = families.get(str(reason["code"]), str(reason["code"]))
        family_weights[family] = max(
            family_weights.get(family, 0.0), float(reason["weight"])
        )
    ordered = sorted(family_weights.values(), reverse=True)
    primary = ordered[0]
    supporting = min(1.0, sum(ordered[1:]))
    return round(min(0.99, primary + (1.0 - primary) * 0.15 * supporting), 4)


def _severity(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def analyze_sync_intervals(
    cues: Iterable[Cue | dict[str, Any]],
    speech_intervals_ms: Iterable[Iterable[int | float] | dict[str, Any]],
    *,
    media_duration_ms: int,
    boundary_search_ms: int = 750,
    review_threshold_ms: int = 450,
    low_overlap_ratio: float = 0.35,
    no_overlap_ratio: float = 0.10,
    utterance_join_gap_ms: int = 120,
    orphan_speech_min_ms: int = 300,
) -> dict[str, Any]:
    """Compare subtitle entries with detected speech without changing any timing."""
    if boundary_search_ms < 0:
        raise WorkflowError("Boundary search window cannot be negative")
    if review_threshold_ms <= 0:
        raise WorkflowError("Review threshold must be positive")
    if not 0 <= no_overlap_ratio <= low_overlap_ratio <= 1:
        raise WorkflowError("Overlap ratios must satisfy 0 <= no <= low <= 1")
    if orphan_speech_min_ms <= 0:
        raise WorkflowError("Orphan speech minimum must be positive")

    speech = normalize_intervals_ms(
        speech_intervals_ms,
        duration_ms=media_duration_ms,
    )
    utterances = normalize_intervals_ms(
        speech,
        duration_ms=media_duration_ms,
        join_gap_ms=utterance_join_gap_ms,
    )
    cue_rows: list[dict[str, Any]] = []
    for item in cues:
        if isinstance(item, Cue):
            row = {
                "index": item.index,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "source": item.text,
            }
        else:
            row = {
                "index": int(item["index"]),
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
                "source": str(item.get("source", "")),
            }
        if row["start_ms"] < 0 or row["end_ms"] <= row["start_ms"]:
            raise WorkflowError(f"Cue {row['index']} has invalid timing")
        if row["end_ms"] > media_duration_ms:
            raise WorkflowError(f"Cue {row['index']} extends past the media duration")
        cue_rows.append(row)
    cue_rows.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["index"]))
    if len({row["index"] for row in cue_rows}) != len(cue_rows):
        raise WorkflowError("Cue indexes must be unique")

    subtitle_coverage = normalize_intervals_ms(
        [(row["start_ms"], row["end_ms"]) for row in cue_rows],
        duration_ms=media_duration_ms,
    ) if cue_rows else []
    cue_position_by_index = {
        row["index"]: position for position, row in enumerate(cue_rows)
    }
    speech_cues: list[list[int]] = []
    for start, end in speech:
        speech_cues.append([
            row["index"] for row in cue_rows
            if min(end, row["end_ms"]) > max(start, row["start_ms"])
        ])
    utterance_cues: list[list[int]] = []
    for start, end in utterances:
        utterance_cues.append([
            row["index"] for row in cue_rows
            if min(end, row["end_ms"]) > max(start, row["start_ms"])
        ])

    cue_reports: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    relation_counts: dict[str, int] = {}

    for position, row in enumerate(cue_rows):
        cue_start = row["start_ms"]
        cue_end = row["end_ms"]
        cue_duration = cue_end - cue_start
        overlaps = _range_intersections(cue_start, cue_end, speech)
        utterance_matches = _range_intersections(cue_start, cue_end, utterances)
        speech_overlap_ms = sum(end - start for _, start, end in overlaps)
        overlap_ratio = speech_overlap_ms / cue_duration
        associated_speech_ms = sum(
            speech[interval_index][1] - speech[interval_index][0]
            for interval_index, _, _ in overlaps
        )
        speech_coverage_ratio = (
            speech_overlap_ms / associated_speech_ms if associated_speech_ms else 0.0
        )

        silence_inside = _subtract_covered_range(cue_start, cue_end, speech)
        leading_non_speech_ms = (
            silence_inside[0][1] - silence_inside[0][0]
            if silence_inside and silence_inside[0][0] == cue_start else 0
        )
        trailing_non_speech_ms = (
            silence_inside[-1][1] - silence_inside[-1][0]
            if silence_inside and silence_inside[-1][1] == cue_end else 0
        )
        largest_internal_silence_ms = max(
            (end - start for start, end in silence_inside),
            default=0,
        )
        speech_before = _speech_not_covered_by_subtitles(
            max(0, cue_start - boundary_search_ms),
            cue_start,
            speech,
            subtitle_coverage,
        )
        speech_after = _speech_not_covered_by_subtitles(
            cue_end,
            min(media_duration_ms, cue_end + boundary_search_ms),
            speech,
            subtitle_coverage,
        )
        uncovered_speech_before_ms = sum(end - start for start, end in speech_before)
        uncovered_speech_after_ms = sum(end - start for start, end in speech_after)

        start_boundary_shared = False
        end_boundary_shared = False
        nearest_nonoverlap_start = None
        nearest_nonoverlap_end = None
        if overlaps:
            first_speech_index = overlaps[0][0]
            last_speech_index = overlaps[-1][0]
            start_boundary_shared = any(
                cue_position_by_index[cue_index] < position
                for cue_index in speech_cues[first_speech_index]
            )
            end_boundary_shared = any(
                cue_position_by_index[cue_index] > position
                for cue_index in speech_cues[last_speech_index]
            )
            first_start = speech[first_speech_index][0]
            last_end = speech[last_speech_index][1]
            nearest_start = (
                first_start
                if not start_boundary_shared
                and abs(first_start - cue_start) <= boundary_search_ms
                else None
            )
            nearest_end = (
                last_end
                if not end_boundary_shared
                and abs(last_end - cue_end) <= boundary_search_ms
                else None
            )
            boundary_match_kind = (
                "associated_interval_boundary"
                if nearest_start is not None or nearest_end is not None
                else "associated_interval_context_only"
            )
        else:
            nearest_start = None
            nearest_end = None
            if speech:
                nearest_interval = min(
                    speech,
                    key=lambda interval: (
                        cue_start - interval[1]
                        if interval[1] <= cue_start
                        else interval[0] - cue_end,
                        interval[0],
                        interval[1],
                    ),
                )
                interval_distance = (
                    cue_start - nearest_interval[1]
                    if nearest_interval[1] <= cue_start
                    else nearest_interval[0] - cue_end
                )
                if 0 <= interval_distance <= boundary_search_ms:
                    nearest_nonoverlap_start, nearest_nonoverlap_end = nearest_interval
            boundary_match_kind = (
                "nearest_nonoverlap_interval_context"
                if nearest_nonoverlap_start is not None else "none"
            )
        start_offset = None if nearest_start is None else cue_start - nearest_start
        end_offset = None if nearest_end is None else cue_end - nearest_end

        utterance_indexes = [index for index, _, _ in utterance_matches]
        related_cues = sorted({
            cue_index
            for utterance_index in utterance_indexes
            for cue_index in utterance_cues[utterance_index]
        })
        if not utterance_indexes:
            relation = "no_detected_speech"
        elif len(utterance_indexes) == 1 and len(utterance_cues[utterance_indexes[0]]) == 1:
            relation = "one_speech_one_cue"
        elif len(utterance_indexes) == 1:
            relation = "one_speech_many_cues"
        elif all(len(utterance_cues[index]) == 1 for index in utterance_indexes):
            relation = "many_speech_one_cue"
        else:
            relation = "many_speech_many_cues"
        relation_counts[relation] = relation_counts.get(relation, 0) + 1

        reasons: list[dict[str, Any]] = []
        if overlap_ratio < no_overlap_ratio:
            reasons.append({
                "code": "no_detected_speech_overlap",
                "weight": 0.88,
                "value": round(overlap_ratio, 4),
                "threshold": no_overlap_ratio,
            })
        elif overlap_ratio < low_overlap_ratio:
            shortfall = (low_overlap_ratio - overlap_ratio) / max(low_overlap_ratio, 0.001)
            reasons.append({
                "code": "low_speech_overlap",
                "weight": round(min(0.62, 0.32 + shortfall * 0.30), 4),
                "value": round(overlap_ratio, 4),
                "threshold": low_overlap_ratio,
            })
        meaningful_overlap = overlap_ratio >= no_overlap_ratio
        if meaningful_overlap and leading_non_speech_ms >= review_threshold_ms:
            reasons.append({
                "code": "subtitle_starts_before_speech",
                "weight": _timing_weight(
                    leading_non_speech_ms, review_threshold_ms, base=0.34, maximum=0.62,
                ),
                "value_ms": leading_non_speech_ms,
                "threshold_ms": review_threshold_ms,
            })
        if meaningful_overlap and trailing_non_speech_ms >= review_threshold_ms:
            reasons.append({
                "code": "subtitle_ends_after_speech",
                "weight": _timing_weight(
                    trailing_non_speech_ms, review_threshold_ms, base=0.34, maximum=0.62,
                ),
                "value_ms": trailing_non_speech_ms,
                "threshold_ms": review_threshold_ms,
            })
        internal_silence_threshold = max(900, review_threshold_ms * 2)
        if (
            meaningful_overlap
            and
            largest_internal_silence_ms >= internal_silence_threshold
            and leading_non_speech_ms < largest_internal_silence_ms
            and trailing_non_speech_ms < largest_internal_silence_ms
        ):
            internal_silence_ratio = largest_internal_silence_ms / cue_duration
            internal_weight = _timing_weight(
                largest_internal_silence_ms,
                internal_silence_threshold,
                base=0.22,
                maximum=0.42,
            )
            if largest_internal_silence_ms >= 3000 or internal_silence_ratio >= 0.50:
                internal_weight = max(internal_weight, 0.48)
            reasons.append({
                "code": "long_internal_silence",
                "weight": round(internal_weight, 4),
                "value_ms": largest_internal_silence_ms,
                "value_ratio": round(internal_silence_ratio, 4),
                "threshold_ms": internal_silence_threshold,
            })

        score = _candidate_score(reasons)
        score_adjustments: list[dict[str, Any]] = []
        if relation == "no_detected_speech" and cue_duration <= 1500 and score >= 0.75:
            score_adjustments.append({
                "code": "short_cue_vad_false_negative_risk",
                "original_score": score,
                "maximum_score": 0.74,
            })
            score = 0.74
        neighbor_indexes = [
            cue_rows[index]["index"]
            for index in range(max(0, position - 1), min(len(cue_rows), position + 2))
            if index != position
        ]
        review_cue_indexes = [
            cue_rows[index]["index"]
            for index in range(max(0, position - 2), min(len(cue_rows), position + 3))
        ]
        flags: list[str] = []
        reason_codes = {reason["code"] for reason in reasons}
        if reasons:
            flags.append("timing_review")
        if "no_detected_speech_overlap" in reason_codes:
            flags.append("no_detected_activity_overlap")
        if reason_codes & {
            "subtitle_starts_before_speech",
        }:
            flags.append("start_timing_review")
        if reason_codes & {
            "subtitle_ends_after_speech",
        }:
            flags.append("end_timing_review")
        if relation in {"one_speech_many_cues", "many_speech_many_cues"}:
            flags.append("shared_utterance_group")
        if relation in {"many_speech_one_cue", "many_speech_many_cues"}:
            flags.append("multiple_utterances_in_cue")

        cue_report = {
            "index": row["index"],
            "source": row["source"],
            "subtitle_start_ms": cue_start,
            "subtitle_end_ms": cue_end,
            "subtitle_duration_ms": cue_duration,
            "detected_activity_start_ms": nearest_start,
            "detected_activity_end_ms": nearest_end,
            "start_offset_ms": start_offset,
            "end_offset_ms": end_offset,
            "boundary_match_kind": boundary_match_kind,
            "nearest_nonoverlap_interval_start_ms": nearest_nonoverlap_start,
            "nearest_nonoverlap_interval_end_ms": nearest_nonoverlap_end,
            "offset_sign_convention": (
                "subtitle boundary minus detected speech boundary; positive means subtitle is later"
            ),
            "detected_activity_overlap_ratio": round(overlap_ratio, 4),
            "vad_speech_overlap_ms": speech_overlap_ms,
            "vad_speech_overlap_ratio": round(overlap_ratio, 4),
            "associated_speech_coverage_ratio": round(speech_coverage_ratio, 4),
            "leading_non_speech_ms": leading_non_speech_ms,
            "trailing_non_speech_ms": trailing_non_speech_ms,
            "largest_internal_silence_ms": largest_internal_silence_ms,
            "uncovered_speech_before_ms": uncovered_speech_before_ms,
            "uncovered_speech_after_ms": uncovered_speech_after_ms,
            "matched_speech_interval_indexes": [index + 1 for index, _, _ in overlaps],
            "matched_utterance_indexes": [index + 1 for index in utterance_indexes],
            "relation": relation,
            "related_cue_indexes": related_cues or [row["index"]],
            "neighbor_indexes": neighbor_indexes,
            "review_cue_indexes": review_cue_indexes,
            "start_boundary_evidence": nearest_start is not None,
            "end_boundary_evidence": nearest_end is not None,
            "start_boundary_shared_with_previous_cue": start_boundary_shared,
            "end_boundary_shared_with_next_cue": end_boundary_shared,
            "candidate_score": score,
            "candidate_reasons": reasons,
            "score_adjustments": score_adjustments,
            "flags": flags,
        }
        cue_reports.append(cue_report)
        if reasons:
            candidates.append({
                "candidate_id": f"cue:{row['index']}",
                "type": "cue_timing",
                "anchor_cue_index": row["index"],
                "cue_indexes": [row["index"]],
                "related_cue_indexes": related_cues or [row["index"]],
                "review_cue_indexes": review_cue_indexes,
                "start_ms": max(0, cue_start - boundary_search_ms),
                "end_ms": min(media_duration_ms, cue_end + boundary_search_ms),
                "relation": relation,
                "score": score,
                "severity": _severity(score),
                "reasons": reasons,
                "score_adjustments": score_adjustments,
                "suggested_action": (
                    "check_timing_or_vad_false_negative"
                    if relation == "no_detected_speech"
                    else "inspect_audio_video_with_neighbor_cues"
                ),
                "automatic_timing_change_allowed": False,
            })

    cue_candidates = candidates
    candidates = [item for item in cue_candidates if item["score"] >= 0.45]
    secondary_candidates = [item for item in cue_candidates if item["score"] < 0.45]

    uncovered_speech: list[tuple[int, int]] = []
    for speech_start, speech_end in speech:
        uncovered_speech.extend(
            _subtract_covered_range(speech_start, speech_end, subtitle_coverage)
        )
    uncovered_fragments = [
        (start, end) for start, end in uncovered_speech
        if end - start >= orphan_speech_min_ms
    ]

    def interval_context(start: int, end: int) -> tuple[list[int], list[int]]:
        before = [row for row in cue_rows if row["end_ms"] <= start]
        after = [row for row in cue_rows if row["start_ms"] >= end]
        left = max(before, key=lambda row: (row["end_ms"], row["index"])) if before else None
        right = min(after, key=lambda row: (row["start_ms"], row["index"])) if after else None
        anchors = [row["index"] for row in (left, right) if row is not None]
        review_positions: set[int] = set()
        for cue_index in anchors:
            position = cue_position_by_index[cue_index]
            review_positions.update(range(max(0, position - 2), min(len(cue_rows), position + 3)))
        review_cues = [cue_rows[position]["index"] for position in sorted(review_positions)]
        return anchors, review_cues

    orphan_utterances: list[tuple[int, int, int, int]] = []
    for utterance_index, (start, end) in enumerate(utterances):
        if utterance_cues[utterance_index]:
            continue
        speech_duration = sum(
            overlap_end - overlap_start
            for _, overlap_start, overlap_end in _range_intersections(start, end, speech)
        )
        if speech_duration >= orphan_speech_min_ms:
            orphan_utterances.append((utterance_index, start, end, speech_duration))

    for utterance_index, start, end, speech_duration in orphan_utterances:
        anchors, review_cues = interval_context(start, end)
        if speech_duration < 600:
            weight = 0.44
        elif speech_duration < 1000:
            weight = 0.62
        else:
            weight = min(0.90, 0.82 + (speech_duration - 1000) / 10000)
        reasons = [{
            "code": "speech_without_subtitle",
            "weight": round(weight, 4),
            "value_ms": speech_duration,
            "threshold_ms": orphan_speech_min_ms,
        }]
        score = _candidate_score(reasons)
        candidates.append({
            "candidate_id": f"speech-without-cue:{utterance_index + 1}",
            "type": "speech_without_subtitle",
            "anchor_cue_index": None,
            "cue_indexes": anchors,
            "related_cue_indexes": anchors,
            "review_cue_indexes": review_cues,
            "start_ms": start,
            "end_ms": end,
            "relation": "speech_without_cue",
            "score": score,
            "severity": _severity(score),
            "reasons": reasons,
            "suggested_action": "inspect_for_missing_or_shifted_subtitle",
            "automatic_timing_change_allowed": False,
        })

    for fragment_index, (start, end) in enumerate(uncovered_fragments, 1):
        parent_indexes = [
            index for index, (utterance_start, utterance_end) in enumerate(utterances)
            if min(end, utterance_end) > max(start, utterance_start)
        ]
        if parent_indexes and all(not utterance_cues[index] for index in parent_indexes):
            continue
        anchors, review_cues = interval_context(start, end)
        related = sorted({
            cue_index
            for parent_index in parent_indexes
            for cue_index in utterance_cues[parent_index]
        })
        duration = end - start
        parent_speech_duration = sum(
            overlap_end - overlap_start
            for parent_index in parent_indexes
            for _, overlap_start, overlap_end in _range_intersections(
                utterances[parent_index][0], utterances[parent_index][1], speech
            )
        )
        fragment_ratio = duration / max(parent_speech_duration, 1)
        is_significant_fragment = duration >= 1000 or fragment_ratio >= 0.50
        reason_code = (
            "large_uncovered_fragment_in_captioned_utterance"
            if is_significant_fragment else "shared_utterance_uncovered_fragment"
        )
        weight = (
            min(0.82, 0.52 + max(0, duration - 1000) / 10000)
            if is_significant_fragment
            else min(0.35, 0.20 + duration / 10000)
        )
        reasons = [{
            "code": reason_code,
            "weight": round(weight, 4),
            "value_ms": duration,
            "value_ratio": round(fragment_ratio, 4),
            "threshold_ms": orphan_speech_min_ms,
        }]
        score = _candidate_score(reasons)
        fragment_candidate = {
            "candidate_id": f"shared-fragment:{fragment_index}",
            "type": (
                "possible_missing_or_shifted_subtitle"
                if is_significant_fragment else "shared_utterance_uncovered_fragment"
            ),
            "anchor_cue_index": None,
            "cue_indexes": anchors,
            "related_cue_indexes": related or anchors,
            "review_cue_indexes": review_cues,
            "start_ms": start,
            "end_ms": end,
            "relation": "utterance_already_has_subtitle",
            "score": score,
            "severity": _severity(score),
            "reasons": reasons,
            "suggested_action": (
                "inspect_for_missing_or_shifted_subtitle"
                if is_significant_fragment
                else "review_only_if_primary_candidate_nearby"
            ),
            "automatic_timing_change_allowed": False,
        }
        if is_significant_fragment:
            candidates.append(fragment_candidate)
        else:
            secondary_candidates.append(fragment_candidate)

    boundaries: list[dict[str, Any]] = []
    for left, right in zip(cue_rows, cue_rows[1:]):
        gap_start = left["end_ms"]
        gap_end = right["start_ms"]
        speech_in_gap_ms = 0
        if gap_end > gap_start:
            speech_in_gap_ms = sum(
                end - start
                for _, start, end in _range_intersections(gap_start, gap_end, speech)
            )
        shared_utterances = [
            index for index, cue_indexes in enumerate(utterance_cues)
            if left["index"] in cue_indexes and right["index"] in cue_indexes
        ]
        boundary_flags: list[str] = []
        if speech_in_gap_ms >= orphan_speech_min_ms:
            boundary_flags.append("speech_in_subtitle_gap")
        if gap_end < gap_start:
            boundary_flags.append("subtitle_overlap")
        if shared_utterances:
            boundary_flags.append("boundary_inside_shared_utterance")
        boundaries.append({
            "left_cue_index": left["index"],
            "right_cue_index": right["index"],
            "subtitle_gap_ms": gap_end - gap_start,
            "speech_in_positive_gap_ms": speech_in_gap_ms,
            "shared_utterance_indexes": [index + 1 for index in shared_utterances],
            "flags": boundary_flags,
        })

    utterance_groups = [
        {
            "index": index + 1,
            "start_ms": start,
            "end_ms": end,
            "cue_indexes": utterance_cues[index],
            "relation": (
                "speech_without_cue" if not utterance_cues[index]
                else "one_speech_one_cue" if len(utterance_cues[index]) == 1
                else "one_speech_many_cues"
            ),
            "automatic_boundary_snap_allowed": False,
        }
        for index, (start, end) in enumerate(utterances)
    ]
    candidates.sort(key=lambda item: (-item["score"], item["start_ms"], item["candidate_id"]))
    secondary_candidates.sort(
        key=lambda item: (-item["score"], item["start_ms"], item["candidate_id"])
    )
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    for candidate in candidates:
        severity_counts[candidate["severity"]] += 1
    secondary_severity_counts = {"high": 0, "medium": 0, "low": 0}
    for candidate in secondary_candidates:
        secondary_severity_counts[candidate["severity"]] += 1
    cue_with_any_reason_count = sum(
        bool(item["candidate_reasons"]) for item in cue_reports
    )
    primary_cue_candidate_count = sum(
        item["type"] == "cue_timing" for item in candidates
    )
    secondary_cue_candidate_count = sum(
        item["type"] == "cue_timing" for item in secondary_candidates
    )
    orphan_intervals = [
        [start, end] for _, start, end, _ in orphan_utterances
    ]

    return {
        "comparison_parameters": {
            "boundary_search_ms": boundary_search_ms,
            "review_threshold_ms": review_threshold_ms,
            "low_overlap_ratio": low_overlap_ratio,
            "no_overlap_ratio": no_overlap_ratio,
            "utterance_join_gap_ms": utterance_join_gap_ms,
            "orphan_speech_min_ms": orphan_speech_min_ms,
            "score_method": "maximum reason plus at most 15 percent residual support",
            "primary_cue_candidate_minimum_score": 0.45,
        },
        "speech_intervals_ms": [[start, end] for start, end in speech],
        "utterance_groups": utterance_groups,
        "silence_intervals_ms": [
            [start, end]
            for start, end in complement_intervals_ms(speech, duration_ms=media_duration_ms)
        ],
        "orphan_utterance_intervals_ms": orphan_intervals,
        "uncovered_speech_intervals_ms": orphan_intervals,
        "uncovered_speech_fragments_ms": [list(item) for item in uncovered_fragments],
        "compatibility_aliases": {
            "uncovered_speech_intervals_ms": "orphan_utterance_intervals_ms",
        },
        "cues": cue_reports,
        "boundaries": boundaries,
        "candidates": candidates,
        "secondary_candidates": secondary_candidates,
        "summary": {
            "cue_count": len(cue_reports),
            "cue_with_any_reason_count": cue_with_any_reason_count,
            "primary_cue_candidate_count": primary_cue_candidate_count,
            "secondary_cue_candidate_count": secondary_cue_candidate_count,
            "flagged_cue_count": cue_with_any_reason_count,
            "primary_flagged_cue_count": primary_cue_candidate_count,
            "candidate_count": len(candidates),
            "secondary_candidate_count": len(secondary_candidates),
            "candidate_severity_counts": severity_counts,
            "secondary_candidate_severity_counts": secondary_severity_counts,
            "uncovered_speech_interval_count": len(orphan_utterances),
            "uncovered_speech_fragment_count": len(uncovered_fragments),
            "relation_counts": relation_counts,
        },
    }


def _ffmpeg_activity_intervals(
    manifest: dict[str, Any],
    *,
    noise: str,
    min_silence: float,
    duration_ms: int,
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    video = Path(manifest["source"]["video"])
    ffmpeg = Path(manifest["tools"]["ffmpeg"])
    completed = run([
        str(ffmpeg), "-hide_banner", "-nostats", "-i", str(video),
        "-map", "0:a:0",
        "-af", f"silencedetect=n={noise}:d={min_silence:g}",
        "-f", "null", "-",
    ], capture=True)
    events = re.findall(r"silence_(start|end):\s*([0-9.]+)", completed.stderr)
    silences: list[tuple[int, int]] = []
    pending: int | None = None
    for event, value in events:
        at = round(float(value) * 1000)
        if event == "start":
            pending = at
        elif pending is not None:
            if at > pending:
                silences.append((pending, at))
            pending = None
        elif at > 0:
            silences.append((0, at))
    if pending is not None and duration_ms > pending:
        silences.append((pending, duration_ms))
    activity = complement_intervals_ms(silences, duration_ms=duration_ms)
    detector = {
        "kind": "ffmpeg-silencedetect",
        "implementation": "FFmpeg silencedetect complement",
        "ffmpeg": str(ffmpeg),
        "parameters": {
            "noise": noise,
            "minimum_silence_seconds": min_silence,
        },
        "warning": (
            "This backend detects non-silence, not speech. Music and noise may be activity."
        ),
    }
    return activity, detector


def _legacy_activity_cue_analysis(
    cues: Iterable[dict[str, Any]],
    activity: list[tuple[int, int]],
    *,
    search_window_ms: int,
    review_threshold_ms: int,
) -> list[dict[str, Any]]:
    """Preserve the schema and non-speech semantics of the original FFmpeg sync report."""
    analysis: list[dict[str, Any]] = []
    for cue in cues:
        start_ms = int(cue["start_ms"])
        end_ms = int(cue["end_ms"])
        overlaps = _range_intersections(start_ms, end_ms, activity)
        overlap_ms = sum(end - start for _, start, end in overlaps)
        overlap_ratio = overlap_ms / max(end_ms - start_ms, 1)
        activity_start = _nearest_boundary(
            (start for start, _ in activity), start_ms, search_window_ms,
        )
        activity_end = _nearest_boundary(
            (end for _, end in activity), end_ms, search_window_ms,
        )
        start_offset = None if activity_start is None else start_ms - activity_start
        end_offset = None if activity_end is None else end_ms - activity_end
        flags: list[str] = []
        if start_offset is not None and abs(start_offset) >= review_threshold_ms:
            flags.append("start_timing_review")
        if end_offset is not None and abs(end_offset) >= review_threshold_ms:
            flags.append("end_timing_review")
        if overlap_ratio < 0.10:
            flags.append("no_detected_activity_overlap")
        analysis.append({
            "index": int(cue["index"]),
            "subtitle_start_ms": start_ms,
            "subtitle_end_ms": end_ms,
            "detected_activity_start_ms": activity_start,
            "detected_activity_end_ms": activity_end,
            "start_offset_ms": start_offset,
            "end_offset_ms": end_offset,
            "detected_activity_overlap_ratio": round(overlap_ratio, 4),
            "start_boundary_evidence": activity_start is not None,
            "end_boundary_evidence": activity_end is not None,
            "flags": flags,
        })
    return analysis


def _silero_vad_intervals(
    manifest: dict[str, Any],
    args: argparse.Namespace,
    *,
    duration_ms: int,
    temporary_root: Path,
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    runtime_root = args.runtime_root.expanduser().resolve()
    python = whisper_runtime_python(runtime_root, args.runtime_python)
    if not python.is_file():
        raise WorkflowError(
            f"Whisper runtime is not installed: {python}. "
            "Run 'python subflow.py whisper-doctor --install-runtime' only after download approval."
        )
    if not WHISPER_WORKER.is_file():
        raise WorkflowError(f"Whisper worker is missing: {WHISPER_WORKER}")

    artifacts = manifest.get("artifacts", {})
    prepared_audio_value = artifacts.get("audio")
    prepared_audio_sha256 = artifacts.get("audio_sha256")
    prepared_audio = Path(prepared_audio_value) if prepared_audio_value else None
    retained_audio = (
        prepared_audio is not None
        and prepared_audio.is_file()
        and isinstance(prepared_audio_sha256, str)
        and bool(prepared_audio_sha256)
    )
    if retained_audio:
        audio = prepared_audio.resolve()
        actual_audio_sha256 = sha256(audio)
        if actual_audio_sha256 != prepared_audio_sha256:
            raise WorkflowError(f"Prepared audio changed after prepare: {audio}")
        expected_audio_sha256 = actual_audio_sha256
    else:
        audio = temporary_root / "audio_16k_mono.wav"
        ffmpeg = Path(manifest["tools"]["ffmpeg"])
        run([
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(Path(manifest["source"]["video"])),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(audio),
        ])
        expected_audio_sha256 = sha256(audio)

    worker_output = temporary_root / "silero-vad-result.json"
    command = [
        str(python), "-X", "utf8", str(WHISPER_WORKER), "vad",
        "--input", str(audio),
        "--output-json", str(worker_output),
        "--threshold", str(args.vad_threshold),
        "--neg-threshold", str(args.vad_neg_threshold),
        "--min-speech-ms", str(args.vad_min_speech_ms),
        "--min-silence-ms", str(args.vad_min_silence_ms),
        "--speech-pad-ms", str(args.vad_speech_pad_ms),
    ]
    if args.vad_max_speech_seconds is not None:
        command.extend(["--max-speech-seconds", str(args.vad_max_speech_seconds)])
    run(command)
    worker = json.loads(worker_output.read_text(encoding="utf-8"))
    if worker.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError("Unsupported Silero VAD worker result schema")
    worker_input = worker.get("input") or {}
    try:
        worker_input_path = Path(str(worker_input["path"])).resolve()
    except (KeyError, OSError, ValueError) as exc:
        raise WorkflowError("Silero VAD worker returned an invalid input path") from exc
    if str(worker_input_path).casefold() != str(audio.resolve()).casefold():
        raise WorkflowError("Silero VAD worker analyzed an unexpected audio path")
    if worker_input.get("sha256") != expected_audio_sha256:
        raise WorkflowError("Silero VAD worker audio hash does not match the submitted audio")
    if int(worker_input.get("sampling_rate_hz", 0)) != 16000:
        raise WorkflowError("Silero VAD worker did not analyze 16 kHz audio")
    worker_detector = worker.get("detector") or {}
    if worker_detector.get("kind") != "silero-vad":
        raise WorkflowError("Silero VAD worker returned the wrong detector kind")
    worker_parameters = worker.get("parameters") or {}
    expected_parameters = {
        "threshold": args.vad_threshold,
        "neg_threshold": args.vad_neg_threshold,
        "min_speech_duration_ms": args.vad_min_speech_ms,
        "min_silence_duration_ms": args.vad_min_silence_ms,
        "speech_pad_ms": args.vad_speech_pad_ms,
        "max_speech_duration_s": args.vad_max_speech_seconds,
    }
    for key, expected_value in expected_parameters.items():
        actual_value = worker_parameters.get(key)
        if isinstance(expected_value, float):
            if actual_value is None or not math.isclose(
                float(actual_value), expected_value, rel_tol=0.0, abs_tol=1e-9,
            ):
                raise WorkflowError(f"Silero VAD worker parameter mismatch: {key}")
        elif actual_value != expected_value:
            raise WorkflowError(f"Silero VAD worker parameter mismatch: {key}")
    raw_intervals = worker.get("speech_intervals_ms")
    if not isinstance(raw_intervals, list):
        raise WorkflowError("Silero VAD worker did not return speech intervals")
    worker_duration = int(worker.get("input", {}).get("duration_ms", 0))
    duration_delta_ms = duration_ms - worker_duration
    allowed_truncated_tail_ms = int(
        getattr(args, "allow_truncated_audio_tail_ms", 0) or 0
    )
    last_cue_end_ms = max(
        (int(cue["end_ms"]) for cue in manifest.get("cues", [])),
        default=0,
    )
    truncated_tail_accepted = (
        retained_audio
        and duration_delta_ms > 1000
        and duration_delta_ms <= allowed_truncated_tail_ms
        and last_cue_end_ms <= worker_duration
    )
    if abs(duration_delta_ms) > 1000 and not truncated_tail_accepted:
        raise WorkflowError(
            f"VAD audio duration differs from manifest by more than 1 second: "
            f"{worker_duration} ms vs {duration_ms} ms"
        )
    intervals = normalize_intervals_ms(raw_intervals, duration_ms=duration_ms)
    detector = dict(worker_detector)
    detector.update({
        "runtime_python": str(python),
        "runtime_root": str(runtime_root),
        "input_audio": worker.get("input"),
        "input_audio_retained": retained_audio,
        "input_audio_manifest_hash_verified": retained_audio,
        "prepared_audio_ignored_without_manifest_hash": (
            prepared_audio is not None and prepared_audio.is_file() and not prepared_audio_sha256
        ),
        "truncated_audio_tail": {
            "accepted": truncated_tail_accepted,
            "allowed_ms": allowed_truncated_tail_ms,
            "actual_missing_ms": max(0, duration_delta_ms),
            "last_cue_end_ms": last_cue_end_ms,
        },
        "parameters": worker.get("parameters"),
    })
    return intervals, detector


def command_sync(args: argparse.Namespace) -> int:
    """Create advisory speech-to-subtitle timing candidates; never auto-retime."""
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    try:
        duration_seconds = float(manifest["media"]["format"]["duration"])
        if not math.isfinite(duration_seconds):
            raise ValueError("non-finite duration")
        duration_ms = round(duration_seconds * 1000)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise WorkflowError("Manifest does not contain a valid media duration") from exc
    if duration_ms <= 0:
        raise WorkflowError("Media duration must be positive")

    output = args.output.resolve()
    protected_values = [
        manifest_path,
        Path(manifest["source"]["video"]),
        Path(manifest["source"]["subtitle"]),
    ]
    prepared_audio_value = manifest.get("artifacts", {}).get("audio")
    if prepared_audio_value:
        protected_values.append(Path(prepared_audio_value))
    protected = {str(path.expanduser().resolve()).casefold() for path in protected_values}
    if str(output).casefold() in protected:
        raise WorkflowError("Sync report output must not overwrite a manifest or source artifact")
    if output.exists():
        if output.is_dir():
            raise WorkflowError(f"Sync report output is a directory: {output}")
        if not args.force:
            raise WorkflowError(f"Refusing to overwrite existing sync report: {output}")
        try:
            previous_report = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                "--force may replace only an existing sync report bound to this manifest"
            ) from exc
        allowed_methods = {
            "FFmpeg silencedetect audio-activity heuristic",
            "Silero VAD v5 speech-to-subtitle timing audit",
        }
        backend_methods = {
            "ffmpeg": "FFmpeg silencedetect audio-activity heuristic",
            "silero": "Silero VAD v5 speech-to-subtitle timing audit",
        }
        if (
            not isinstance(previous_report, dict)
            or previous_report.get("manifest_id") != manifest["manifest_id"]
            or previous_report.get("method") not in allowed_methods
            or previous_report.get("sync_schema_version") not in {1, 2}
            or previous_report.get("advisory_only") is not True
            or previous_report.get("timing_changes_applied") is not False
            or backend_methods.get(previous_report.get("backend"))
            != previous_report.get("method")
            or not isinstance(previous_report.get("cues"), list)
        ):
            raise WorkflowError(
                "--force may replace only an existing sync report bound to this manifest"
            )

    if args.backend == "ffmpeg":
        activity, detector = _ffmpeg_activity_intervals(
            manifest,
            noise=args.noise,
            min_silence=args.min_silence,
            duration_ms=duration_ms,
        )
        cue_analysis = _legacy_activity_cue_analysis(
            manifest["cues"],
            activity,
            search_window_ms=round(args.search_window * 1000),
            review_threshold_ms=args.review_threshold_ms,
        )
        report = {
            "schema_version": SCHEMA_VERSION,
            "sync_schema_version": 1,
            "created_at": utc_now(),
            "manifest": str(manifest_path),
            "manifest_id": manifest["manifest_id"],
            "backend": "ffmpeg",
            "method": "FFmpeg silencedetect audio-activity heuristic",
            "warning": (
                "Activity is the complement of detected silence, not speech recognition. "
                "Background music/noise can resemble speech. Agent review is required."
            ),
            "advisory_only": True,
            "timing_changes_applied": False,
            "detector": detector,
            "parameters": {
                "noise": args.noise,
                "minimum_silence_seconds": args.min_silence,
                "search_window_seconds": args.search_window,
                "review_threshold_ms": args.review_threshold_ms,
            },
            "silence_intervals_ms": [
                list(item)
                for item in complement_intervals_ms(activity, duration_ms=duration_ms)
            ],
            "activity_intervals_ms": [list(item) for item in activity],
            "cues": cue_analysis,
        }
        write_json_atomic(output, report)
        flagged = sum(bool(item["flags"]) for item in cue_analysis)
        print(f"Sync evidence (ffmpeg): {output} ({flagged}/{len(cue_analysis)} cues flagged)")
        return 0

    with tempfile.TemporaryDirectory(prefix="subflow-sync-") as temporary:
        temporary_root = Path(temporary)
        detected, detector = _silero_vad_intervals(
            manifest,
            args,
            duration_ms=duration_ms,
            temporary_root=temporary_root,
        )
        method = "Silero VAD v5 speech-to-subtitle timing audit"
        warning = (
            "Silero VAD estimates likely speech regions; it does not know subtitle meaning or "
            "the correct cue boundary. Candidates require audio/video and neighbor-cue review."
        )

        analysis = analyze_sync_intervals(
            manifest["cues"],
            detected,
            media_duration_ms=duration_ms,
            boundary_search_ms=round(args.search_window * 1000),
            review_threshold_ms=args.review_threshold_ms,
            low_overlap_ratio=args.low_overlap_ratio,
            no_overlap_ratio=args.no_overlap_ratio,
            utterance_join_gap_ms=args.utterance_join_gap_ms,
            orphan_speech_min_ms=args.orphan_speech_min_ms,
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "sync_schema_version": 2,
        "created_at": utc_now(),
        "manifest": str(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "source": {
            "video": manifest["source"]["video"],
            "video_sha256": manifest["source"]["video_sha256"],
            "subtitle": manifest["source"]["subtitle"],
            "subtitle_sha256": manifest["source"]["subtitle_sha256"],
            "media_duration_ms": duration_ms,
        },
        "backend": args.backend,
        "method": method,
        "warning": warning,
        "advisory_only": True,
        "timing_changes_applied": False,
        "detector": detector,
        "parameters": {
            "backend": args.backend,
            "detection": detector.get("parameters", {}),
            "comparison": analysis["comparison_parameters"],
        },
        "activity_intervals_ms": analysis["speech_intervals_ms"],
        "activity_intervals_ms_alias_of": "speech_intervals_ms",
        "candidate_tiers": {
            "primary": "ranked review queue",
            "secondary": "low-score or already-captioned utterance context",
        },
        **analysis,
    }
    write_json_atomic(output, report)
    primary_cues = report["summary"]["primary_cue_candidate_count"]
    total = report["summary"]["cue_count"]
    candidate_count = report["summary"]["candidate_count"]
    secondary_count = report["summary"]["secondary_candidate_count"]
    print(
        f"Sync evidence ({args.backend}): {output} "
        f"({primary_cues}/{total} primary cue candidates, {candidate_count} primary and "
        f"{secondary_count} secondary candidates)"
    )
    return 0


def command_merge(args: argparse.Namespace) -> int:
    if not (args.parts or getattr(args, "translation_map", None)):
        raise WorkflowError("merge requires --parts or --translation-map")
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    source_cues = {int(item["index"]): item for item in manifest["cues"]}
    merged: dict[int, dict[str, Any]] = {}
    origins: dict[int, str] = {}
    reference_docs = set(getattr(args, "reference_doc", None) or [])

    def ingest(part_paths: list[Path], *, replace_existing: bool, preserve_source: bool) -> None:
        for part_path in part_paths:
            ingest_one(part_path, replace_existing=replace_existing, preserve_source=preserve_source)

    def ingest_one(part_path: Path, *, replace_existing: bool, preserve_source: bool) -> None:
        path = part_path.resolve()
        payload = json.loads(path.read_text(encoding="utf-8"))
        for doc_id in payload.get("reference_docs", []):
            if not isinstance(doc_id, str) or not doc_id.strip():
                raise WorkflowError(f"Invalid reference_docs entry in {path}")
            reference_docs.add(doc_id.strip())
        for item in payload.get("decisions", []):
            index = int(item["index"])
            if index not in source_cues:
                raise WorkflowError(f"{path} references unknown cue {index}")
            if index in merged and not replace_existing:
                raise WorkflowError(f"Cue {index} occurs in both {origins[index]} and {path}")
            normalized = dict(merged[index]) if index in merged and replace_existing else {}
            normalized.update(item)
            normalized["index"] = index
            normalized["source"] = source_cues[index]["source"]
            normalized["corrected"] = (
                normalized["source"]
                if preserve_source
                else str(normalized.get("corrected") or normalized["source"]).strip()
            )
            normalized["translation"] = str(normalized.get("translation") or "").strip()
            normalized.setdefault("confidence", "reviewed")
            normalized.setdefault("notes", "")
            merged[index] = normalized
            origins[index] = str(path)

    def ingest_translation_map(map_paths: list[Path]) -> None:
        for map_path in map_paths:
            path = map_path.resolve()
            payload = json.loads(path.read_text(encoding="utf-8"))
            for doc_id in payload.get("reference_docs", []):
                if not isinstance(doc_id, str) or not doc_id.strip():
                    raise WorkflowError(f"Invalid reference_docs entry in {path}")
                reference_docs.add(doc_id.strip())
            translations = payload.get("translations", payload)
            if not isinstance(translations, dict):
                raise WorkflowError(f"Translation map must be an object in {path}")
            for raw_index, translation in translations.items():
                try:
                    index = int(raw_index)
                except (TypeError, ValueError) as exc:
                    raise WorkflowError(f"Invalid cue index {raw_index!r} in {path}") from exc
                if index not in source_cues:
                    raise WorkflowError(f"{path} references unknown cue {index}")
                if index in merged:
                    raise WorkflowError(f"Cue {index} occurs in both {origins[index]} and {path}")
                if not isinstance(translation, str) or not translation.strip():
                    raise WorkflowError(f"Cue {index} in {path} has an empty translation")
                merged[index] = {
                    "index": index,
                    "source": source_cues[index]["source"],
                    "corrected": source_cues[index]["source"],
                    "translation": translation.strip(),
                    "confidence": "reviewed",
                    "notes": "",
                }
                origins[index] = str(path)

    ingest(args.parts or [], replace_existing=False, preserve_source=args.source_preserving)
    ingest_translation_map(getattr(args, "translation_map", None) or [])
    ingest(args.overrides or [], replace_existing=True, preserve_source=False)
    missing = sorted(set(source_cues) - set(merged))
    if missing and not args.allow_missing:
        raise WorkflowError(f"Missing {len(missing)} cue decisions; first: {missing[:10]}")
    for index in missing:
        merged[index] = {
            "index": index,
            "source": source_cues[index]["source"],
            "corrected": source_cues[index]["source"],
            "translation": "",
            "confidence": "unreviewed",
            "notes": "",
        }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "source_language": manifest.get("source_language", "en"),
        "target_language": manifest.get("target_language", "ko"),
        "reference_docs": sorted(reference_docs),
        "translation_style": {
            "register": "natural tutorial narration",
            "preserve_technical_terms": True,
            "max_korean_line_chars": 24,
            "preferred_max_lines": 2,
            "overflow_policy": "condense first; split only with timing evidence",
        },
        "decisions": [merged[index] for index in sorted(merged)],
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Merged {len(merged)} cue decisions: {output}")
    return 0


def load_decisions(path: Path, manifest: dict[str, Any], manifest_path: Path) -> dict[int, dict[str, Any]]:
    path = path.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {int(item["index"]): item for item in manifest["cues"]}
    result: dict[int, dict[str, Any]] = {}
    for item in payload.get("decisions", []):
        index = int(item["index"])
        if index in result:
            raise WorkflowError(f"Duplicate decision for cue {index}")
        if index not in expected:
            raise WorkflowError(f"Decision references unknown cue {index}")
        result[index] = item
    return result


def command_apply(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    source_cues = parse_srt(Path(manifest["source"]["subtitle"]))
    decisions = load_decisions(args.decisions.resolve(), manifest, manifest_path)
    expected = {cue.index for cue in source_cues}
    missing = expected - set(decisions)
    extra = set(decisions) - expected
    if extra:
        raise WorkflowError(f"Decisions reference unknown cues: {sorted(extra)}")
    if missing and not args.allow_missing:
        raise WorkflowError(f"Missing decisions for {len(missing)} cues; first: {sorted(missing)[:10]}")

    corrected: list[Cue] = []
    translated: list[Cue] = []
    bilingual: list[Cue] = []
    changes: list[dict[str, Any]] = []
    for cue in source_cues:
        decision = decisions.get(cue.index, {})
        corrected_text = str(decision.get("corrected") or cue.text).strip()
        translation = str(decision.get("translation") or "").strip()
        if not translation and not args.allow_missing:
            raise WorkflowError(f"Cue {cue.index} has an empty translation")
        if not translation:
            translation = corrected_text
        timing = decision.get("timing") or {}
        if not isinstance(timing, dict):
            raise WorkflowError(f"Cue {cue.index} timing must be an object")
        start_ms = int(timing.get("start_ms", cue.start_ms))
        end_ms = int(timing.get("end_ms", cue.end_ms))
        if start_ms < 0 or end_ms <= start_ms:
            raise WorkflowError(f"Cue {cue.index} has invalid timing: {start_ms}..{end_ms}")
        corrected_text = split_lines(corrected_text, args.source_line_chars, max_lines=2)
        translation = split_lines(translation, args.target_line_chars, max_lines=2)
        corrected_cue = replace(cue, start_ms=start_ms, end_ms=end_ms, text=corrected_text)
        translated_cue = replace(cue, start_ms=start_ms, end_ms=end_ms, text=translation)
        corrected.append(corrected_cue)
        translated.append(translated_cue)
        bilingual.append(replace(cue, start_ms=start_ms, end_ms=end_ms, text=f"{corrected_text}\n{translation}".rstrip()))
        if corrected_text.replace("\n", " ") != cue.text.replace("\n", " ") or start_ms != cue.start_ms or end_ms != cue.end_ms:
            changes.append({
                "index": cue.index,
                "original": cue.text,
                "corrected": corrected_text,
                "original_timing": [cue.start_ms, cue.end_ms],
                "corrected_timing": [start_ms, end_ms],
                "notes": decision.get("notes", ""),
            })

    previous = None
    duration = manifest.get("media", {}).get("format", {}).get("duration")
    media_duration_ms = int(float(duration) * 1000) if duration else None
    for cue in corrected:
        if previous is not None and cue.start_ms < previous.end_ms:
            raise WorkflowError(f"Timing decision creates an overlap at cue {cue.index}")
        if media_duration_ms is not None and cue.end_ms > media_duration_ms + 250:
            raise WorkflowError(f"Cue {cue.index} ends after the media")
        previous = cue

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_language = str(manifest["source_language"])
    target_language = str(manifest["target_language"])
    write_srt(output / f"corrected.{source_language}.srt", corrected)
    write_srt(output / f"translated.{target_language}.srt", translated)
    write_srt(output / f"bilingual.{source_language}-{target_language}.srt", bilingual)
    change_payload = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "decisions_sha256": sha256(args.decisions.resolve()),
        "created_at": utc_now(),
        "change_count": len(changes),
        "changes": changes,
    }
    (output / "changes.json").write_text(
        json.dumps(change_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(source_cues)} cues to {output}; {len(changes)} source/timing changes")
    return 0


def inspect_output(
    cues: list[Cue], *, media_duration_ms: int | None, mode: str,
    target_language: str,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    previous = None
    for cue in cues:
        if not cue.text.strip():
            errors.append({"index": cue.index, "code": "empty_text"})
        if cue.end_ms <= cue.start_ms:
            errors.append({"index": cue.index, "code": "invalid_duration"})
        if previous and cue.start_ms < previous.end_ms:
            errors.append({"index": cue.index, "code": "overlap", "milliseconds": previous.end_ms - cue.start_ms})
        if media_duration_ms is not None and cue.end_ms > media_duration_ms + 250:
            errors.append({"index": cue.index, "code": "past_media_end"})
        if mode != "bilingual":
            compact = re.sub(r"\s+", "", cue.text)
            cps = len(compact) / max(cue.duration_ms / 1000, 0.001)
            threshold = 16 if mode == "target" else 25
            if cps > threshold:
                warnings.append({"index": cue.index, "code": "high_reading_speed", "cps": round(cps, 2)})
            if mode == "target" and target_language == "ko" and not re.search(r"[가-힣]", cue.text):
                errors.append({"index": cue.index, "code": "no_hangul"})
        previous = cue

    run_start = 0
    while run_start < len(cues):
        normalized = re.sub(r"\s+", " ", cues[run_start].text).strip().casefold()
        run_end = run_start + 1
        while run_end < len(cues):
            candidate = re.sub(r"\s+", " ", cues[run_end].text).strip().casefold()
            if candidate != normalized:
                break
            run_end += 1
        repeated = cues[run_start:run_end]
        if len(repeated) >= 3 and any(cue.duration_ms < 500 for cue in repeated):
            errors.append({
                "index": repeated[0].index,
                "end_index": repeated[-1].index,
                "code": "suspicious_repeated_micro_cues",
                "count": len(repeated),
                "minimum_duration_ms": min(cue.duration_ms for cue in repeated),
            })
        run_start = run_end
    return {"cue_count": len(cues), "errors": errors, "warnings": warnings}


def command_verify(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(args.manifest.resolve())
    duration = manifest.get("media", {}).get("format", {}).get("duration")
    media_duration_ms = int(float(duration) * 1000) if duration else None
    source_language = str(manifest["source_language"])
    target_language = str(manifest["target_language"])
    files = {
        "corrected": (output / f"corrected.{source_language}.srt", "source"),
        "translated": (output / f"translated.{target_language}.srt", "target"),
        "bilingual": (output / f"bilingual.{source_language}-{target_language}.srt", "bilingual"),
    }
    results: dict[str, Any] = {}
    parsed: dict[str, list[Cue]] = {}
    expected_indices = [int(item["index"]) for item in manifest["cues"]]
    for name, (path, mode) in files.items():
        if not path.is_file():
            results[name] = {
                "cue_count": 0,
                "errors": [{"code": "missing_file", "path": str(path)}], "warnings": [],
            }
            continue
        try:
            cues = parse_srt(path)
        except (WorkflowError, OSError, ValueError) as exc:
            results[name] = {
                "cue_count": 0,
                "errors": [{"code": "parse_error", "message": str(exc)}], "warnings": [],
            }
            continue
        parsed[name] = cues
        result = inspect_output(
            cues, media_duration_ms=media_duration_ms, mode=mode,
            target_language=target_language,
        )
        actual_indices = [cue.index for cue in cues]
        if actual_indices != expected_indices:
            mismatch = next(
                (position for position, pair in enumerate(zip(actual_indices, expected_indices), 1) if pair[0] != pair[1]),
                min(len(actual_indices), len(expected_indices)) + 1,
            )
            result["errors"].append({
                "code": "cue_index_sequence_mismatch", "position": mismatch,
                "expected_count": len(expected_indices), "actual_count": len(actual_indices),
            })
        results[name] = result

    baseline = parsed.get("corrected")
    if baseline is not None:
        baseline_timing = [(cue.index, cue.start_ms, cue.end_ms) for cue in baseline]
        for name in ("translated", "bilingual"):
            other = parsed.get(name)
            if other is None:
                continue
            other_timing = [(cue.index, cue.start_ms, cue.end_ms) for cue in other]
            if other_timing != baseline_timing:
                results[name]["errors"].append({"code": "cross_file_timing_or_index_mismatch"})
    if baseline is not None and "translated" in parsed and "bilingual" in parsed:
        for corrected_cue, translated_cue, bilingual_cue in zip(
            baseline, parsed["translated"], parsed["bilingual"]
        ):
            expected_text = f"{corrected_cue.text}\n{translated_cue.text}"
            if bilingual_cue.text != expected_text:
                results["bilingual"]["errors"].append({
                    "index": bilingual_cue.index, "code": "bilingual_content_mismatch",
                })
    error_count = sum(len(item["errors"]) for item in results.values())
    warning_count = sum(len(item["warnings"]) for item in results.values())
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "created_at": utc_now(),
        "ok": error_count == 0,
        "error_count": error_count,
        "warning_count": warning_count,
        "results": results,
    }
    report_path = output / "verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = [
        "# Subtitle verification", "",
        f"- Result: {'PASS' if report['ok'] else 'FAIL'}",
        f"- Errors: {error_count}", f"- Warnings: {warning_count}", "",
    ]
    for name, result in results.items():
        markdown.extend([
            f"## {name}", "",
            f"- Cues: {result.get('cue_count', 'n/a')}",
            f"- Errors: {len(result['errors'])}",
            f"- Warnings: {len(result['warnings'])}", "",
        ])
    (output / "verification.md").write_text("\n".join(markdown), encoding="utf-8")
    print(f"Verification {'PASS' if report['ok'] else 'FAIL'}: {report_path}")
    return 0 if report["ok"] else 1


def command_publish(args: argparse.Namespace) -> int:
    """Publish verified final artifacts under output/YYYY-MM-DD/HHmmss."""
    manifest_path = args.manifest.resolve()
    decisions_path = args.decisions.resolve()
    source_output = args.source_output.resolve()
    output_root = args.output_root.resolve()
    manifest = load_manifest(manifest_path)
    decisions = load_decisions(decisions_path, manifest, manifest_path)
    expected_indexes = {int(item["index"]) for item in manifest["cues"]}
    if set(decisions) != expected_indexes:
        missing = sorted(expected_indexes - set(decisions))
        extra = sorted(set(decisions) - expected_indexes)
        raise WorkflowError(f"Publish requires complete decisions; missing={missing[:10]}, extra={extra[:10]}")
    empty_translations = [index for index, item in decisions.items() if not str(item.get("translation") or "").strip()]
    if empty_translations:
        raise WorkflowError(f"Publish requires complete translations; empty cues: {empty_translations[:10]}")
    changes_path = source_output / "changes.json"
    try:
        changes = json.loads(changes_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Publish requires a valid apply record: {changes_path}") from exc
    if not isinstance(changes, dict):
        raise WorkflowError(f"Publish apply record must be an object: {changes_path}")
    if changes.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(f"Publish apply record has an unsupported schema: {changes_path}")
    if changes.get("manifest_id") != manifest["manifest_id"]:
        raise WorkflowError("Rendered subtitles belong to a different manifest")
    if command_verify(argparse.Namespace(manifest=manifest_path, output=source_output)) != 0:
        raise WorkflowError("Refusing to publish because verification failed")

    if args.timestamp:
        try:
            published_at = datetime.fromisoformat(args.timestamp)
        except ValueError as exc:
            raise WorkflowError("--timestamp must be ISO-8601, for example 2026-07-11T22:30:45+09:00") from exc
        if published_at.tzinfo is None:
            published_at = published_at.astimezone()
    else:
        published_at = datetime.now().astimezone()

    date_dir = output_root / published_at.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    base_name = published_at.strftime("%H%M%S")
    run_dir = date_dir / base_name
    suffix = 1
    while run_dir.exists():
        run_dir = date_dir / f"{base_name}-{suffix:02d}"
        suffix += 1
    subtitles_dir = run_dir / "subtitles"
    reports_dir = run_dir / "reports"
    previews_dir = run_dir / "previews"
    metadata_dir = run_dir / "metadata"
    for directory in (subtitles_dir, reports_dir, previews_dir, metadata_dir):
        directory.mkdir(parents=True, exist_ok=True)

    source_language = str(manifest["source_language"])
    target_language = str(manifest["target_language"])
    copied: list[Path] = []

    def copy_required(source: Path, destination: Path) -> None:
        if not source.is_file():
            raise WorkflowError(f"Required publish artifact is missing: {source}")
        shutil.copy2(source, destination)
        copied.append(destination)

    def copy_optional(source: Path, destination: Path) -> None:
        if source.is_file():
            shutil.copy2(source, destination)
            copied.append(destination)

    copy_required(
        source_output / f"corrected.{source_language}.srt",
        subtitles_dir / f"corrected.{source_language}.srt",
    )
    copy_required(
        source_output / f"translated.{target_language}.srt",
        subtitles_dir / f"translated.{target_language}.srt",
    )
    copy_required(
        source_output / f"bilingual.{source_language}-{target_language}.srt",
        subtitles_dir / f"bilingual.{source_language}-{target_language}.srt",
    )
    for name in ("changes.json", "verification.json", "verification.md"):
        copy_required(source_output / name, reports_dir / name)
    report_path = args.report.resolve() if args.report else manifest_path.parent / "REPORT.md"
    copy_optional(report_path, reports_dir / "REPORT.md")
    for preview in sorted(source_output.glob("preview_*.png")):
        copy_optional(preview, previews_dir / preview.name)
    copy_required(manifest_path, metadata_dir / "manifest.json")
    copy_required(decisions_path, metadata_dir / "decisions.json")
    copy_optional(manifest_path.parent / "sync_analysis.json", metadata_dir / "sync_analysis.json")
    copy_optional(manifest_path.parent / "evidence" / "packet.json", metadata_dir / "evidence.packet.json")

    source_sidecar: Path | None = None
    if not args.no_source_sidecar:
        source_sidecar = subtitle_sidecar_path(
            Path(manifest["source"]["video"]),
            target_language,
        )
        copy_file_atomic(
            source_output / f"translated.{target_language}.srt",
            source_sidecar,
        )

    verification = json.loads((source_output / "verification.json").read_text(encoding="utf-8"))
    run_record = {
        "schema_version": SCHEMA_VERSION,
        "published_at": published_at.isoformat(timespec="seconds"),
        "manifest_id": manifest["manifest_id"],
        "source": manifest["source"],
        "source_workdir": str(manifest_path.parent),
        "source_output": str(source_output),
        "publish_directory": str(run_dir),
        "source_sidecar": None if source_sidecar is None else {
            "path": str(source_sidecar),
            "sha256": sha256(source_sidecar),
        },
        "reference_docs": json.loads(decisions_path.read_text(encoding="utf-8")).get("reference_docs", []),
        "verification": {
            "ok": bool(verification.get("ok")),
            "error_count": int(verification.get("error_count", 0)),
            "warning_count": int(verification.get("warning_count", 0)),
        },
        "artifacts": [path.relative_to(run_dir).as_posix() for path in copied],
    }
    run_json = run_dir / "run.json"
    run_json.write_text(json.dumps(run_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = {
        "published_at": run_record["published_at"],
        "manifest_id": manifest["manifest_id"],
        "run_directory": str(run_dir),
        "source_sidecar": None if source_sidecar is None else str(source_sidecar),
    }
    (output_root / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Published: {run_dir}")
    if source_sidecar is not None:
        print(f"Source sidecar: {source_sidecar}")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    """Compare a second ASR SRT against source cues by temporal overlap."""
    manifest = load_manifest(args.manifest.resolve())
    source = parse_srt(Path(manifest["source"]["subtitle"]))
    reference = parse_srt(args.reference.resolve())
    comparisons = []
    for cue in source:
        overlapping = [
            other for other in reference
            if min(cue.end_ms, other.end_ms) - max(cue.start_ms, other.start_ms) > 0
        ]
        reference_text = " ".join(item.text.replace("\n", " ") for item in overlapping).strip()
        similarity = SequenceMatcher(
            None, cue.text.lower().replace("\n", " "), reference_text.lower()
        ).ratio() if reference_text else 0.0
        comparisons.append({
            "index": cue.index,
            "source": cue.text,
            "reference": reference_text,
            "similarity": round(similarity, 4),
            "needs_review": similarity < args.threshold,
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": manifest["manifest_id"],
        "reference": str(args.reference.resolve()),
        "threshold": args.threshold,
        "comparisons": comparisons,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Compared {len(comparisons)} cues: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subflow", description="FFmpeg-backed, agent-in-the-loop subtitle correction and translation"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_whisper_transcription_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--output", type=Path, required=True, help="output SRT path")
        command.add_argument("--metadata", type=Path, help="optional transcription metadata JSON path")
        command.add_argument("--runtime-root", type=Path, default=DEFAULT_WHISPER_RUNTIME_ROOT)
        command.add_argument("--runtime-python", type=Path, help="override the isolated runtime Python")
        command.add_argument("--model-root", type=Path, default=DEFAULT_WHISPER_MODEL_ROOT)
        command.add_argument("--model", default="large-v3-turbo")
        command.add_argument("--language", help="ISO-639-1 language code, for example en or ko")
        command.add_argument("--device", default="cuda", choices=("cuda", "cpu", "auto"))
        command.add_argument("--compute-type", default="float16")
        command.add_argument("--beam-size", type=int, default=5)
        command.add_argument("--vad-filter", action="store_true")
        command.add_argument("--word-timestamps", action="store_true")
        command.add_argument("--no-condition-on-previous-text", action="store_true")
        command.add_argument("--initial-prompt")
        command.add_argument(
            "--local-files-only", action="store_true",
            help="fail instead of downloading a missing model",
        )
        command.add_argument("--keep-audio", action="store_true", help="preserve the extracted 16 kHz WAV")
        command.add_argument("--ffmpeg-root", type=Path)
        command.add_argument("--force", action="store_true")

    doctor = sub.add_parser("doctor", help="check required local tools")
    doctor.add_argument("--ffmpeg-root", type=Path, help="optional explicit FFmpeg root; PATH is searched by default")
    doctor.add_argument(
        "--install-ffmpeg", action="store_true",
        help="download and install FFmpeg when it is not available",
    )
    doctor.add_argument("--ffmpeg-download-url", default=DEFAULT_FFMPEG_DOWNLOAD_URL)
    doctor.add_argument("--ffmpeg-sha256", help="optional expected archive SHA-256")
    doctor.add_argument(
        "--skip-download-checksum", action="store_true",
        help="skip checksum verification (not recommended)",
    )
    doctor.set_defaults(func=command_doctor)

    whisper_doctor = sub.add_parser(
        "whisper-doctor",
        help="check or install the project-local faster-whisper runtime",
    )
    whisper_doctor.add_argument("--runtime-root", type=Path, default=DEFAULT_WHISPER_RUNTIME_ROOT)
    whisper_doctor.add_argument("--runtime-python", type=Path)
    whisper_doctor.add_argument("--model-root", type=Path, default=DEFAULT_WHISPER_MODEL_ROOT)
    whisper_doctor.add_argument("--install-runtime", action="store_true")
    whisper_doctor.add_argument("--upgrade-runtime", action="store_true")
    whisper_doctor.set_defaults(func=command_whisper_doctor)

    youtube_doctor = sub.add_parser(
        "youtube-doctor",
        help="check or install the project-local yt-dlp runtime",
    )
    youtube_doctor.add_argument("--runtime-root", type=Path, default=DEFAULT_YOUTUBE_RUNTIME_ROOT)
    youtube_doctor.add_argument("--runtime-python", type=Path)
    youtube_doctor.add_argument("--install-runtime", action="store_true")
    youtube_doctor.add_argument("--upgrade-runtime", action="store_true")
    youtube_doctor.set_defaults(func=command_youtube_doctor)

    download_youtube = sub.add_parser(
        "download-youtube",
        help="download YouTube media only; all YouTube subtitles are forcibly disabled",
    )
    download_youtube.add_argument("url")
    download_youtube.add_argument("--output-dir", type=Path, default=Path.cwd())
    download_youtube.add_argument("--runtime-root", type=Path, default=DEFAULT_YOUTUBE_RUNTIME_ROOT)
    download_youtube.add_argument("--runtime-python", type=Path)
    download_youtube.add_argument("--ffmpeg-root", type=Path)
    download_youtube.add_argument("--output-template", default="%(title)s [%(id)s].%(ext)s")
    download_youtube.add_argument("--format", default="bv*+ba/b")
    download_youtube.add_argument("--container", default="mp4", choices=("mp4", "mkv", "webm"))
    download_youtube.add_argument("--audio-only", action="store_true")
    download_youtube.add_argument("--audio-format", default="mp3", choices=("mp3", "m4a", "opus", "wav", "flac"))
    download_youtube.add_argument("--playlist", action="store_true", help="allow downloading every item in a playlist URL")
    download_youtube.add_argument("--force", action="store_true", help="overwrite existing downloaded files")
    download_youtube.set_defaults(func=command_download_youtube)

    transcribe = sub.add_parser(
        "transcribe",
        help="transcribe a full media file or an exact time range with faster-whisper",
    )
    transcribe.add_argument("media", type=Path)
    transcribe.add_argument("--start", help="seconds, MM:SS.mmm, or HH:MM:SS.mmm")
    transcribe.add_argument("--end", help="seconds, MM:SS.mmm, or HH:MM:SS.mmm")
    add_whisper_transcription_options(transcribe)
    transcribe.set_defaults(func=command_transcribe)

    transcribe_cues = sub.add_parser(
        "transcribe-cues",
        help="retranscribe the time envelope around selected manifest cues",
    )
    transcribe_cues.add_argument("--manifest", type=Path, required=True)
    transcribe_cues.add_argument("--cues", required=True, help="nearby cue indexes, for example 8-12")
    transcribe_cues.add_argument("--padding", type=float, default=1.25)
    add_whisper_transcription_options(transcribe_cues)
    transcribe_cues.set_defaults(func=command_transcribe_cues)

    prepare = sub.add_parser("prepare", help="probe media and create review artifacts")
    prepare.add_argument("video", type=Path)
    prepare.add_argument("subtitle", type=Path)
    prepare.add_argument("--workdir", type=Path, required=True)
    prepare.add_argument("--ffmpeg-root", type=Path, help="optional explicit FFmpeg root; PATH is searched by default")
    prepare.add_argument("--source-language", default="en")
    prepare.add_argument("--target-language", default="ko")
    prepare.add_argument("--no-audio", action="store_true")
    prepare.add_argument("--no-frames", action="store_true")
    prepare.add_argument("--frame-interval", type=float, default=10.0)
    prepare.add_argument(
        "--force", action="store_true",
        help="atomically replace a non-empty workdir created by subflow",
    )
    prepare.set_defaults(func=command_prepare)

    evidence = sub.add_parser("evidence", help="extract WAV, frame, and review MP4 for selected cues")
    evidence.add_argument("--manifest", type=Path, required=True)
    evidence.add_argument("--cues", required=True, help="for example: 3,8-12,42")
    evidence.add_argument("--output", type=Path, required=True)
    evidence.add_argument("--padding", type=float, default=1.25)
    evidence.add_argument("--context", type=int, default=2)
    evidence.set_defaults(func=command_evidence)

    sync = sub.add_parser(
        "sync",
        help="compare subtitle entries with FFmpeg activity or Silero VAD speech evidence",
    )
    sync.add_argument("--manifest", type=Path, required=True)
    sync.add_argument("--output", type=Path, required=True)
    sync.add_argument(
        "--backend", choices=("ffmpeg", "silero"), default="ffmpeg",
        help="keep legacy FFmpeg activity analysis or opt into Silero speech detection",
    )
    sync.add_argument("--noise", default="-25dB")
    sync.add_argument("--min-silence", type=float, default=0.25)
    sync.add_argument("--search-window", type=float, default=0.75)
    sync.add_argument("--review-threshold-ms", type=int, default=450)
    sync.add_argument("--low-overlap-ratio", type=float, default=0.35)
    sync.add_argument("--no-overlap-ratio", type=float, default=0.10)
    sync.add_argument("--utterance-join-gap-ms", type=int, default=120)
    sync.add_argument("--orphan-speech-min-ms", type=int, default=300)
    sync.add_argument("--runtime-root", type=Path, default=DEFAULT_WHISPER_RUNTIME_ROOT)
    sync.add_argument("--runtime-python", type=Path)
    sync.add_argument("--vad-threshold", type=float, default=0.50)
    sync.add_argument("--vad-neg-threshold", type=float, default=0.35)
    sync.add_argument("--vad-min-speech-ms", type=int, default=100)
    sync.add_argument("--vad-min-silence-ms", type=int, default=250)
    sync.add_argument("--vad-speech-pad-ms", type=int, default=100)
    sync.add_argument("--vad-max-speech-seconds", type=float)
    sync.add_argument(
        "--allow-truncated-audio-tail-ms",
        type=int,
        default=0,
        help=(
            "accept a shorter hash-bound prepared WAV only when every cue ends before "
            "the WAV; intended for a damaged or silent media tail"
        ),
    )
    sync.add_argument("--force", action="store_true", help="replace an existing report file")
    sync.set_defaults(func=command_sync)

    merge = sub.add_parser("merge", help="merge agent decision JSON parts")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--parts", type=Path, nargs="*", help="decision JSON parts")
    merge.add_argument("--translation-map", type=Path, nargs="*", help="compact JSON maps of cue index to translation")
    merge.add_argument("--overrides", type=Path, nargs="*", help="review files applied after parts; later values win")
    merge.add_argument(
        "--reference-doc", action="append",
        help="indexed doc ID used by the agent; repeat for multiple documents",
    )
    merge.add_argument(
        "--source-preserving", action="store_true",
        help="ignore source edits proposed by translation parts; use --overrides for confirmed corrections",
    )
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--allow-missing", action="store_true")
    merge.set_defaults(func=command_merge)

    compare = sub.add_parser("compare", help="compare source SRT with a second ASR SRT")
    compare.add_argument("--manifest", type=Path, required=True)
    compare.add_argument("--reference", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--threshold", type=float, default=0.72)
    compare.set_defaults(func=command_compare)

    apply_cmd = sub.add_parser("apply", help="apply agent decisions and create final subtitles")
    apply_cmd.add_argument("--manifest", type=Path, required=True)
    apply_cmd.add_argument("--decisions", type=Path, required=True)
    apply_cmd.add_argument("--output", type=Path, required=True)
    apply_cmd.add_argument("--source-line-chars", type=int, default=42)
    apply_cmd.add_argument("--target-line-chars", type=int, default=24)
    apply_cmd.add_argument("--allow-missing", action="store_true")
    apply_cmd.set_defaults(func=command_apply)

    verify = sub.add_parser("verify", help="validate generated subtitle artifacts")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.set_defaults(func=command_verify)

    publish = sub.add_parser("publish", help="copy verified artifacts into output/YYYY-MM-DD/HHmmss")
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--decisions", type=Path, required=True)
    publish.add_argument("--source-output", type=Path, required=True)
    publish.add_argument("--output-root", type=Path, required=True)
    publish.add_argument("--report", type=Path)
    publish.add_argument("--timestamp", help="optional ISO-8601 timestamp for reproducible/backfilled publishing")
    publish.add_argument(
        "--no-source-sidecar", action="store_true",
        help="do not copy translated.<target>.srt next to the source video as <video>.<target>.srt",
    )
    publish.set_defaults(func=command_publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except subprocess.CalledProcessError as exc:
        print(f"External command failed ({exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return 2
    except (
        WorkflowError, OSError, ValueError, TypeError, KeyError, AttributeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
