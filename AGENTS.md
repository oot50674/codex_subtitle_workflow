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
- Submit every full or partial Whisper transcription through
  `scripts/retranscription_queue.ps1`; do not launch overlapping direct
  `subflow.py transcribe` processes. Enqueue focused cue evidence with
  `-JobType transcribe-cues -Manifest <path> -Cues <selection>` rather than
  invoking it directly. Partial SRT timestamps are restored to the original
  media timeline. After an interrupted worker, use `-Action recover` only after
  confirming no worker is active.
- For a multi-file batch, jobs may be enqueued in advance, but drain one initial
  full-file job at a time with `-Action drain -MaxJobs 1`. After each job
  completes, run `prepare`, then translate and review that file before draining
  the next job unless the user requests a different order.
- After each file's full transcription and `prepare`, run
  `scripts/silero_sync_audit.ps1` when timing review is in scope. It shares the
  global media-worker lock with the transcription queue, creates candidates
  only, and must never rewrite cue timings. It is not a prerequisite for
  translation.
- Keep Silero media-duration matching strict. Use
  `-AllowTruncatedAudioTailMs` only for a SHA-256-bound prepared WAV whose
  missing tail begins after the final cue, and document the damaged media tail.
- After approved explicit timing overrides, create a comparison artifact with
  `scripts/finalize_sync_subtitles.ps1`. Its default display padding is 300 ms
  before and 800 ms after each approved override, configurable up to 3000 ms.
  It proportionally reduces padding in short gaps, never moves untargeted
  cues, refuses double padding, and runs `apply` plus `verify` before exposing
  the new output directory.
- Keep only reusable, job-independent utilities under `scripts/`. Put scripts
  written for one current video, series, or batch under `temp/`.
- Store each job in a new work directory and publish final artifacts under
  `output/YYYY-MM-DD/HHmmss`.
- By default, `publish` also writes the translated subtitle beside the source
  video as `<video-stem>.<target-language>.srt` (for example `video.ko.srt`).
  Use `--no-source-sidecar` only when the user explicitly does not want this.
- Prefer one or two subtitle lines. Condense before splitting a cue, and only
  retime or split when audio/video evidence supports the change.
- Before a related job, consult `doc/index.json`; after verification, create a
  job record and update both `doc/index.json` and `doc/INDEX.md`.
