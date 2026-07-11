# Subtitle workflow agent entrypoint

- Read `AGENT_PROTOCOL.md` completely before reviewing or translating subtitles.
- Use PowerShell Core (`pwsh`) for Windows commands.
- Run `subflow.py doctor` first. FFmpeg must be discovered from `PATH`,
  `SUBFLOW_FFMPEG_ROOT`, or `FFMPEG_ROOT`; do not assume a fixed drive path.
- If FFmpeg is missing, use `subflow.py doctor --install-ffmpeg` only with the
  user's authorization for the download and local installation.
- Keep media operations deterministic in the CLI. The agent owns semantic
  correction, translation, timing judgment, and evidence notes.
- Store each job in a new work directory and publish final artifacts under
  `output/YYYY-MM-DD/HHmmss`.
- Prefer one or two subtitle lines. Condense before splitting a cue, and only
  retime or split when audio/video evidence supports the change.
- Before a related job, consult `doc/index.json`; after verification, create a
  job record and update both `doc/index.json` and `doc/INDEX.md`.
