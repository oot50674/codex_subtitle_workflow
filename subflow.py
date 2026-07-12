#!/usr/bin/env python3
"""Agent-in-the-loop subtitle review workflow.

The CLI owns deterministic media/subtitle work.  A language-capable agent owns
correction and translation decisions written to decisions.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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


def split_lines(text: str, max_chars: int) -> str:
    """Balance wrapped lines without collapsing internal whitespace."""
    if not text.strip() or max_chars <= 0:
        return text.strip()
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


def command_sync(args: argparse.Namespace) -> int:
    """Use FFmpeg audio activity boundaries as evidence, never as an auto-retimer."""
    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    video = Path(manifest["source"]["video"])
    ffmpeg = Path(manifest["tools"]["ffmpeg"])
    duration_s = float(manifest["media"]["format"]["duration"])
    completed = run([
        str(ffmpeg), "-hide_banner", "-nostats", "-i", str(video),
        "-map", "0:a:0",
        "-af", f"silencedetect=n={args.noise}:d={args.min_silence:g}",
        "-f", "null", "-",
    ], capture=True)
    events = re.findall(r"silence_(start|end):\s*([0-9.]+)", completed.stderr)
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for event, value in events:
        at = float(value)
        if event == "start":
            pending = at
        elif pending is not None:
            silences.append((pending, at))
            pending = None
        else:
            silences.append((0.0, at))
    if pending is not None:
        silences.append((pending, duration_s))

    speech: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(silences):
        if start > cursor:
            speech.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_s:
        speech.append((cursor, duration_s))

    analysis = []
    for cue in manifest["cues"]:
        start_s = cue["start_ms"] / 1000
        end_s = cue["end_ms"] / 1000
        overlaps = [max(0.0, min(end_s, b) - max(start_s, a)) for a, b in speech]
        speech_overlap_s = sum(overlaps)
        overlap_ratio = speech_overlap_s / max(end_s - start_s, 0.001)
        start_candidates = [a for a, _ in speech if abs(a - start_s) <= args.search_window]
        end_candidates = [b for _, b in speech if abs(b - end_s) <= args.search_window]
        flags = []
        speech_start = min(start_candidates, key=lambda value: abs(value - start_s)) if start_candidates else None
        speech_end = min(end_candidates, key=lambda value: abs(value - end_s)) if end_candidates else None
        if speech_start is not None:
            start_offset = round((start_s - speech_start) * 1000)
            if abs(start_offset) >= args.review_threshold_ms:
                flags.append("start_timing_review")
        else:
            start_offset = None
        if speech_end is not None:
            end_offset = round((end_s - speech_end) * 1000)
            if abs(end_offset) >= args.review_threshold_ms:
                flags.append("end_timing_review")
        else:
            end_offset = None
        if overlap_ratio < 0.10:
            flags.append("no_detected_activity_overlap")
        analysis.append({
            "index": cue["index"],
            "subtitle_start_ms": cue["start_ms"],
            "subtitle_end_ms": cue["end_ms"],
            "detected_activity_start_ms": None if speech_start is None else round(speech_start * 1000),
            "detected_activity_end_ms": None if speech_end is None else round(speech_end * 1000),
            "start_offset_ms": start_offset,
            "end_offset_ms": end_offset,
            "detected_activity_overlap_ratio": round(overlap_ratio, 4),
            "start_boundary_evidence": speech_start is not None,
            "end_boundary_evidence": speech_end is not None,
            "flags": flags,
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "manifest": str(manifest_path),
        "manifest_id": manifest["manifest_id"],
        "method": "FFmpeg silencedetect audio-activity heuristic",
        "warning": "Activity is the complement of detected silence, not speech recognition. Background music/noise can resemble speech. Agent review is required before timing changes.",
        "parameters": {
            "noise": args.noise,
            "minimum_silence_seconds": args.min_silence,
            "search_window_seconds": args.search_window,
            "review_threshold_ms": args.review_threshold_ms,
        },
        "silence_intervals_ms": [[round(a * 1000), round(b * 1000)] for a, b in silences],
        "activity_intervals_ms": [[round(a * 1000), round(b * 1000)] for a, b in speech],
        "cues": analysis,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flagged = sum(bool(item["flags"]) for item in analysis)
    print(f"Sync evidence: {output} ({flagged}/{len(analysis)} cues flagged)")
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
            expected_source = re.sub(r"\s+", " ", source_cues[index]["source"]).strip()
            supplied_source = re.sub(r"\s+", " ", str(item.get("source", ""))).strip()
            if not supplied_source and not replace_existing:
                raise WorkflowError(f"Cue {index} in {path} is missing its source evidence")
            if supplied_source and supplied_source != expected_source:
                raise WorkflowError(f"Source mismatch for cue {index} in {path}")
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
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise WorkflowError(f"Unsupported decisions schema: {payload.get('schema_version')}")
    if payload.get("manifest_id") != manifest["manifest_id"]:
        raise WorkflowError(f"Decisions belong to a different manifest: {path}")
    decision_manifest = payload.get("manifest")
    if not decision_manifest or Path(decision_manifest).resolve() != manifest_path.resolve():
        raise WorkflowError(f"Decisions reference a different manifest path: {path}")
    if payload.get("source_language") != manifest["source_language"]:
        raise WorkflowError("Decision source language does not match the manifest")
    if payload.get("target_language") != manifest["target_language"]:
        raise WorkflowError("Decision target language does not match the manifest")
    expected = {int(item["index"]): item for item in manifest["cues"]}
    result: dict[int, dict[str, Any]] = {}
    for item in payload.get("decisions", []):
        index = int(item["index"])
        if index in result:
            raise WorkflowError(f"Duplicate decision for cue {index}")
        if index not in expected:
            raise WorkflowError(f"Decision references unknown cue {index}")
        if "source" not in item or str(item["source"]) != str(expected[index]["source"]):
            raise WorkflowError(f"Decision source mismatch at cue {index}")
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
        corrected_text = split_lines(corrected_text, args.source_line_chars)
        translation = split_lines(translation, args.target_line_chars)
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

    sync = sub.add_parser("sync", help="create speech/silence timing evidence for agent review")
    sync.add_argument("--manifest", type=Path, required=True)
    sync.add_argument("--output", type=Path, required=True)
    sync.add_argument("--noise", default="-25dB")
    sync.add_argument("--min-silence", type=float, default=0.25)
    sync.add_argument("--search-window", type=float, default=0.75)
    sync.add_argument("--review-threshold-ms", type=int, default=450)
    sync.set_defaults(func=command_sync)

    merge = sub.add_parser("merge", help="merge agent decision JSON parts")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--parts", type=Path, nargs="*", help="decision JSON parts with explicit source evidence")
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
