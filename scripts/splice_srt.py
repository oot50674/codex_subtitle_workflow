from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


STAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


@dataclass
class Cue:
    start: int
    end: int
    text: str


def to_ms(value: str) -> int:
    match = STAMP.fullmatch(value.strip())
    if not match:
        raise ValueError(f"invalid timestamp: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + millis


def stamp(value: int) -> str:
    hours, rest = divmod(value, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def read_srt(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if " --> " in line), None)
        if timing_index is None:
            continue
        start, end = lines[timing_index].split(" --> ", 1)
        cues.append(Cue(to_ms(start), to_ms(end), "\n".join(lines[timing_index + 1 :]).strip()))
    return cues


def write_srt(path: Path, cues: list[Cue]) -> None:
    blocks = []
    for index, cue in enumerate(sorted(cues, key=lambda item: (item.start, item.end)), 1):
        blocks.append(f"{index}\n{stamp(cue.start)} --> {stamp(cue.end)}\n{cue.text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--replacement", type=Path, required=True)
    parser.add_argument("--start-ms", type=int, required=True)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = read_srt(args.base)
    replacement = read_srt(args.replacement)
    kept = [cue for cue in base if cue.end <= args.start_ms or cue.start >= args.end_ms]
    inserted = [cue for cue in replacement if cue.end > args.start_ms and cue.start < args.end_ms]
    if not inserted:
        raise SystemExit("replacement contains no cues in requested range")
    write_srt(args.output, kept + inserted)


if __name__ == "__main__":
    main()
