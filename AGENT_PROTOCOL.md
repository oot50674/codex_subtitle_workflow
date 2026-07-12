# Subtitle review agent protocol

The agent is the language and context decision-maker. The CLI is only the
media, bookkeeping, and rendering layer.

## Inputs to inspect

- `manifest.json`: authoritative cue text, timing, media metadata, and basic
  structural flags.
- `sync_analysis.json`: advisory audio-activity boundaries. Never apply these
  offsets mechanically.
- `media/frames`: periodic visual context.
- `evidence/packet.json` plus its WAV/JPG/MP4 files: close audio and moving
  visual evidence for selected cues.
- `*.transcription.json`: model, source hash, original-timeline range, options,
  segment confidence, and optional word timestamps for Whisper runs.
- The two cues before and after every correction candidate.

## Whisper-assisted transcription

1. Run `subflow.py whisper-doctor` before invoking Whisper. Install the
   project-local runtime or download a missing model only with user approval.
2. When no draft SRT exists, use `transcribe` on the full media. When checking
   a doubtful passage, transcribe the smallest useful time range or use
   `transcribe-cues` on nearby manifest cues.
3. Treat Whisper output as additional evidence, not as automatic truth. Check
   surrounding cues and current audio/video before correcting source text.
4. Partial transcription SRT timestamps are already rebased to the original
   media timeline. Do not offset them a second time.
5. Keep `*.transcription.json` with the job evidence so the model, range,
   options, source hash, and confidence values remain auditable.

## Document memory and indexing

`AGENT_PROTOCOL.md` contains procedure only. Reusable knowledge and individual
work history belong in the separate [`doc/`](doc/) library:

- `doc/jobs/`: one verified record per video or manifest-bound job
- `doc/series/`: continuity across episodes, creators, or one ongoing project
- `doc/domains/`: reusable terminology and judgment rules for similar domains
- `doc/index.json`: authoritative machine-readable index
- `doc/INDEX.md`: matching human-readable index

### Before every job

1. Derive a stable job slug, `continuity_key`, domain tags, and language pair
   from the current inputs. Do not infer a continuity key from a vague visual
   resemblance alone; use a shared series, creator, project, or explicit user
   context.
2. Read `doc/index.json` before reviewing cues. Use `doc/INDEX.md` only as the
   human-readable companion, not as a substitute for the JSON index.
3. Select the smallest relevant document set in this priority order:

   - the `series` document with an exact `continuity_key` match;
   - directly preceding or explicitly related `job` documents;
   - `domain` documents sharing the strongest domain-tag match;
   - verified similar `job` documents when concrete examples are useful.

4. Record every selected `doc_id` in the new job document's `references` and
   pass it to `merge --reference-doc`. State briefly what was reused or why the
   current evidence required a different decision.
5. If nothing matches, proceed from current evidence and create the missing
   domain or series document only after reusable knowledge has actually been
   established.

### Evidence precedence and conflicts

Use this precedence order:

1. current video, audio, on-screen UI, and current manifest;
2. current evidence packets and neighboring cues;
3. current draft SRT;
4. prior verified job documents;
5. series and domain guidance.

Never copy an old correction merely because tags match. If a current source
conflicts with an indexed document, follow current evidence, record the
divergence in the new job document, and update the reusable document only when
the new conclusion is stable beyond this one cue.

### After every job

1. Create or update a job record from `doc/templates/JOB_RECORD.md`.
2. For the same manifest or a direct continuation of the same unfinished job,
   update the existing job document and add a dated revision note. For a new
   episode in the same series, create a new job document with the same
   `continuity_key`. For an unrelated but similar task, create a new job
   document and link only the relevant domain document.
3. Mark a job `verified` only after `verify` passes. Add its manifest ID and
   timestamped published run after `publish` succeeds.
4. Confirm that the translated sidecar was written beside the source video as
   `<video-stem>.<target-language>.srt`, and record that path in the job. Skip
   it with `--no-source-sidecar` only when explicitly requested.
5. Promote only reusable terminology, repeated ASR patterns, stable names, and
   continuity decisions into `series` or `domain` documents. Do not copy full
   transcripts, cue-by-cue translations, or temporary guesses into them.
6. Update `doc/index.json` and `doc/INDEX.md` in the same change. Keep `doc_id`
   values unique, paths relative to `doc/`, and cross-references resolvable in
   both directions.
7. Preserve older verified records. Correct them with a dated revision note or
   mark them superseded; do not silently rewrite history.

## Decision rules

1. Preserve the spoken meaning. Do not rewrite merely to make the source text
   more formal.
2. Correct a source cue only when audio, on-screen UI, surrounding context, or
   stable domain terminology supports the correction.
3. Keep source timing unless evidence shows a real boundary error. A subtitle
   appearing a little before speech is normal. Never use silence-detection
   offsets as an automatic replacement.
4. Translate ideas, tone, jokes, and tutorial intent rather than English word
   order. Keep each cue understandable with its immediate neighbors.
5. Keep Blender shortcuts as explicit key sequences (`Ctrl+R`, `G` twice,
   `M → A`, `M → L`, `K`, `E`, `F`).
6. Keep each translated cue to one or two lines whenever possible. Use roughly
   24 Korean characters per line as the default guide. If a cue would exceed
   two lines, first shorten or rephrase it without dropping meaning. Split the
   cue only when the speech timing and surrounding context support a clean
   split. Use three or more lines only as an unavoidable exception and explain
   the reason in `notes`.
7. Use the same Korean term throughout a video. For this sample:

   - mesh → 메시
   - vertex/vert → 버텍스
   - edge/face → 엣지/페이스
   - topology → 토폴로지
   - loop cut → 루프 컷
   - transition triangle → 전환 삼각형
   - extrude → 익스트루드
   - sculpt → 스컬프트
   - subdivision → 서브디비전
   - N-gon → 엔곤(N-gon) on first use, then 엔곤
   - three-quarter angle → 3/4 뷰

8. Record a short `notes` explanation for non-obvious source or timing changes.
9. Use `confidence: high|medium|low`. Low-confidence decisions must retain the
   original timing and be listed for a final human check.

## Required completion checks

- Exactly one decision for every manifest cue.
- `source` matches the manifest text.
- No empty `corrected` or `translation` fields.
- Translated cues are normally one or two lines; every longer exception has an
  explicit reason in `notes`.
- Every correction candidate has been checked against nearby context and, when
  useful, a frame or evidence packet.
- `apply` succeeds without `--allow-missing`.
- `verify` reports zero errors. Reading-speed warnings are review prompts, not
  automatic failures.
- `publish` places the verified deliverables under
  `output/YYYY-MM-DD/HHmmss` and writes a bound `run.json` record.
- Unless explicitly disabled, `publish` writes the translated subtitle next to
  the source video as `<video-stem>.<target-language>.srt` and records its path
  and SHA-256 in `run.json`.
- A job record exists under `doc/jobs/`, all referenced `doc_id` values resolve
  through `doc/index.json`, and `doc/INDEX.md` reflects the same documents.
