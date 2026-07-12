# Subtitle workflow agent entrypoint

- Read `AGENT_PROTOCOL.md` completely before reviewing or translating subtitles.
- Use PowerShell Core (`pwsh`) for Windows commands.
- Run `subflow.py doctor` first. FFmpeg must be discovered from `PATH`,
  `SUBFLOW_FFMPEG_ROOT`, or `FFMPEG_ROOT`; do not assume a fixed drive path.
- Before transcription, run `subflow.py whisper-doctor`. Use
  `--install-runtime` only with the user's authorization for dependency
  downloads. The isolated environment and model cache live under
  `.runtime/whisper`.
- If FFmpeg is missing, use `subflow.py doctor --install-ffmpeg` only with the
  user's authorization for the download and local installation.
- Before downloading a YouTube video, run `subflow.py youtube-doctor`. Use
  `--install-runtime` only with the user's authorization for dependency
  downloads. The isolated yt-dlp environment lives under `.runtime/youtube`.
- Use `subflow.py download-youtube` for YouTube URLs. It downloads one video by
  default even when the URL contains a playlist; use `--playlist` only when the
  user explicitly requests the full playlist.
- Never download or use YouTube subtitles or automatic captions as a
  transcription source. `download-youtube` is media-only. When a trusted SRT
  was not supplied by the user, transcribe the full media with
  `large-v3-turbo` before translation.
- Never invoke the project-local `yt-dlp` module directly. All YouTube
  downloads must go through `subflow.py download-youtube`, which forcibly
  disables manual and automatic subtitle downloads.
- Keep media operations deterministic in the CLI. The agent owns semantic
  correction, translation, timing judgment, and evidence notes.
- Use `subflow.py transcribe` for a missing full draft or an exact time range.
  Use `transcribe-cues` for nearby manifest cues that need an ASR cross-check.
  Partial SRT timestamps are restored to the original media timeline.
- Store each job in a new work directory and publish final artifacts under
  `output/YYYY-MM-DD/HHmmss`.
- By default, `publish` also writes the translated subtitle beside the source
  video as `<video-stem>.<target-language>.srt` (for example `video.ko.srt`).
  Use `--no-source-sidecar` only when the user explicitly does not want this.
- Prefer one or two subtitle lines. Condense before splitting a cue, and only
  retime or split when audio/video evidence supports the change.
- Before a related job, consult `doc/index.json`; after verification, create a
  job record and update both `doc/index.json` and `doc/INDEX.md`.
