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

## Silero VAD sync-suspicion audit

Silero VAD is a candidate detector, not a retiming engine. It estimates likely
speech regions without knowing the words, speaker intent, subtitle segmentation,
or the correct reading lead-in/hold time.

### Per-file generation

1. After one file's full transcription and `prepare` have completed, run
   `scripts/silero_sync_audit.ps1 -Manifest <path>` when timing review is in
   scope.
   Readiness is per file; a later file's transcription batch does not need to
   finish first.
2. The reusable script runs `doctor` and `whisper-doctor`, reuses the prepared
   16 kHz mono WAV when available, and invokes the Silero backend through the
   isolated Whisper runtime. It must not install or upgrade the runtime. Any
   missing dependency or model download still requires user approval.
3. The script and `scripts/retranscription_queue.ps1` share one exclusive
   media-worker lock. Do not bypass that lock or start direct worker processes.
   Schedule the audit when the transcription queue is idle; retry later if the
   lock is occupied.
4. Use the sync-audit defaults as a reproducible baseline: speech threshold
   `0.50`, negative threshold `0.35`, minimum speech `100 ms`, minimum silence
   `250 ms`, speech padding `100 ms`, utterance join gap `120 ms`, and timing
   review threshold `450 ms`. These are detection settings, not approved timing
   tolerances.
5. The report must bind itself to the manifest ID, source hashes, audio hash,
   media duration, faster-whisper and ONNX Runtime versions, Silero model asset
   hashes, and every detector/comparison parameter. It must state that no timing
   change was applied.
6. If systematic background music, clicks, breathing, or missing short
   interjections make the baseline clearly unsuitable, write a second report
   with changed parameters instead of silently replacing the baseline. Record
   why the alternate settings were tried; a better-looking score alone is not
   evidence that they are more accurate. Existing reports are not overwritten
   by default. Use a distinct output name; use `-Force` only after preserving
   the earlier report and documenting why replacement is intentional.
7. Keep the media-duration check strict by default. A damaged container may be
   audited with `-AllowTruncatedAudioTailMs` only when a prepared WAV is bound
   by SHA-256, the WAV is shorter only at the tail, and every subtitle cue ends
   before the WAV. Record the allowed and actual missing duration in the sync
   report; never use this exception to ignore missing audio under a cue.

### Candidate interpretation

1. Review the ranked primary `candidates` first, then consult
   `secondary_candidates` only when a nearby primary finding or direct audio
   evidence warrants it. Low-score cue findings and short uncovered fragments
   of an utterance that already has a subtitle stay secondary. A detected
   utterance with no overlapping cue becomes `speech_without_subtitle`; a long
   or proportionally large uncovered fragment becomes
   `possible_missing_or_shifted_subtitle`. `high`, `medium`, or `low` is never an
   automatic correction or approval. Read every reason component and measured
   value.
2. Offset signs are fixed: `start_offset_ms` and `end_offset_ms` are subtitle
   boundary minus detected speech boundary. Positive means the subtitle
   boundary is later; negative means it is earlier.
3. Review these patterns first:

   - a cue with little or no detected speech overlap;
   - long leading or trailing non-speech inside a cue;
   - detected speech just before or after a cue that no neighboring cue covers;
   - detected speech in a positive gap between adjacent subtitle entries;
   - long internal silence that may indicate a shifted, merged, or split cue;
   - a detected speech interval not covered by any subtitle.

4. Relations such as `one_speech_many_cues`, `many_speech_one_cue`, and
   `many_speech_many_cues` are grouping evidence, not errors by themselves. A
   single continuous utterance commonly spans several readable subtitle cues,
   and one cue may legitimately contain a pause. Never snap every cue in such a
   group to the same VAD start/end or independently retime an internal boundary.
5. Background music, sound effects, breaths, clipped interjections, overlapping
   speakers, and Silero's 32 ms analysis resolution can cause false positives or
   negatives. Absence of detected speech is not proof of silence, and detected
   speech is not proof that a subtitle is missing.

### Review and timing approval

1. Review each sync candidate with the two preceding and two following cues,
   every overlapping/nearest VAD interval, the original audio, and moving video
   evidence when useful. Use `evidence` for the smallest useful region. If
   speech content or word placement remains unclear, enqueue focused cue evidence
   through `scripts/retranscription_queue.ps1 -Action enqueue -JobType
   transcribe-cues` with `-Manifest`, `-Cues`, and `-WordTimestamps`, then drain
   the queue before relying on the result. Whisper timestamps remain secondary
   evidence.
2. Classify every reviewed candidate as one of: confirmed misalignment,
   acceptable subtitle lead/hold, VAD false positive, VAD false negative,
   legitimate cue segmentation, or unresolved human check. Do not infer a new
   boundary solely from the nearest VAD edge.
3. Approve a timing change only when the original audio/video and neighboring
   cue continuity support it. For a shared utterance or multi-utterance cue,
   decide the whole group together and preserve readable, non-overlapping cue
   order. Record the approved `timing` pair and a concise evidence note in
   `notes`.
4. Low-confidence or unresolved candidates retain their original timing and are
   listed for final human review. The manifest and source SRT remain unchanged;
   only reviewed decision output may carry accepted timing overrides.

### Final display padding and artifact generation

1. Display padding is a post-review presentation treatment, not timing
   evidence. Run it only after timing review is finished. It may expand only
   decisions that already carry an explicit reviewed `timing` override; it must
   never create a correction from VAD or Whisper boundaries.
2. Use `scripts/finalize_sync_subtitles.ps1` to create the padded final artifact
   in a new output directory. The reusable default is `300 ms` before the
   approved start and `800 ms` after the approved end. Both values are
   configurable from `0` through `3000 ms`; use roughly `200` to `3000 ms` only
   when the viewing style and available gaps support it. Do not confuse this
   display padding with Silero's `vad-speech-pad-ms` detector setting.
3. At every boundary, preserve the reviewed cue order and prohibit overlap. If
   the requested end padding of the left cue plus the requested start padding
   of the right cue exceeds the available gap, reduce both requests
   proportionally. Clip the first or last padded cue to the media boundary.
   Never steal time by moving an untargeted neighbor.
4. Write the padded result to a new decisions file and retain the reviewed,
   unpadded decisions unchanged. A decisions file carrying `timing_padding`
   metadata must not be padded again. Record requested and actually granted
   padding per cue, including every collision or media-boundary reduction.
5. The finalizer runs `doctor`, `apply`, and `verify`, and exposes the output
   directory only after verification succeeds. Keep the padded decisions,
   padding report, rendered SRT variants, and verification artifacts together.

## Whisper-assisted transcription

0. YouTube subtitles and automatic captions are prohibited as transcription
   sources. If the user did not provide a trusted source SRT, run a full-media
   `large-v3-turbo` transcription. Do not substitute downloaded captions for
   Whisper to save time.
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

### Transcription queue and batch workflow

1. A batch may be enqueued in advance, but drain one initial full-file
   transcription at a time with
   `scripts/retranscription_queue.ps1 -Action drain -MaxJobs 1`. As soon as that
   job succeeds, use the returned `processed_jobs` entry to identify its exact
   output and metadata, then run `prepare` before draining the next job.
2. After one file's full transcription and `prepare`, translate every cue and
   perform static ASR candidate triage in one continuous pass for that file. Do
   not wait for the rest of the batch or a Silero report before starting
   translation on a ready file.
3. For multi-file batches, finish translation and review for the current file
   before starting the next unless the user explicitly requests a different
   order.
4. Inspect the completed SRT, manifest, transcription metadata, cue durations,
   repeated text, neighboring cue text, and indexed terminology. Translate every
   cue. Record likely hallucinations, mistranscribed technical terms, or
   passages needing evidence in `notes` or as correction candidates—not as
   approved corrections until verified against audio or video.
5. Every Whisper transcription call, including initial full-file transcription
   and follow-up partial or full retranscription, must be submitted through
   `scripts/retranscription_queue.ps1`. Do not launch an ad-hoc overlapping
   `subflow.py transcribe` process. The queue has one global worker, serializes
   CPU/GPU/FFmpeg work to prevent system overload, and rejects a duplicate
   media/range/options job that is pending, running, or completed.
6. Output and metadata paths are delivery locations, not compute identity. A
   duplicate response points to the first job's existing output, which should be
   reused instead of launching the same computation again. `transcribe-cues`
   jobs retain and revalidate the enqueue-time manifest hash so their actual cue
   envelope cannot drift before execution.
7. If the machine stops while a queue job is reserved or running, do not delete
   queue files by hand. After confirming no worker is active and the stale
   threshold has elapsed, run `scripts/retranscription_queue.ps1 -Action
   recover`; it moves stale running records to failed and releases stale
   identity claims for an audited retry.
8. Silero sync audit is an independent timing-review input, not a prerequisite
   for translation. Schedule it when the media-worker lock is available.

### Translation and review

1. Use the prepared manifest as the source of truth. One decisions file covers
   every cue with explicit `index` and `source` values.
2. Translate through doubtful source text instead of blocking the whole file.
   Preserve the current source, make the best context-aware translation, and
   explain uncertainty in `notes`. Follow all selected terminology, shortcut,
   line-count, and continuity rules.
3. Review every cue against the source language and neighboring context before
   acceptance. Correct mistranslations, omissions, technical terms, tone, line
   breaks, and continuity while checking candidate passages with audio or video
   evidence where needed.
5. After retranscription changes the manifest, regenerate or rebase affected
   decisions and review them against the new manifest.

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
6. Keep each translated cue to one or two lines whenever possible. Break lines
   at natural semantic or syntactic boundaries, not by a fixed character
   count. If a cue would exceed two lines, first shorten or rephrase it without
   dropping meaning. Split the cue only when the speech timing and surrounding
   context support a clean split. Use three or more lines only as an
   unavoidable exception and explain the reason in `notes`.
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
9. Use `confidence: high|medium|low` when it helps track uncertainty. Low-confidence
   decisions should retain the original timing and be listed for a final human check.
10. Treat three or more consecutive identical cues as a mandatory ASR
    hallucination check when any cue is shorter than 500 ms. Check the audio
    through the final cue instead of accepting reading-speed warnings alone.

## Required completion checks

- The source provenance is either a user-supplied trusted SRT or a retained
  `large-v3-turbo` transcription record. YouTube captions are not accepted.
- Exactly one decision for every manifest cue.
- `source` matches the manifest text.
- No empty `corrected` or `translation` fields.
- Translated cues are normally one or two lines; every longer exception has an
  explicit reason in `notes`.
- Every correction candidate has been checked against nearby context and, when
  useful, a frame or evidence packet.
- Repeated micro-cue runs and sub-150 ms cues near the end of the media have
  been checked against audio; unexplained repetitions are not publishable.
- `apply` succeeds without `--allow-missing`.
- `verify` reports zero errors. Reading-speed warnings are review prompts, not
  automatic failures.
- When timing overrides are padded, the finalizer uses a new output directory,
  no cue overlaps, and the reviewed decisions remain unchanged.
- `publish` places the verified deliverables under
  `output/YYYY-MM-DD/HHmmss` and writes a bound `run.json` record.
- Unless explicitly disabled, `publish` writes the translated subtitle next to
  the source video as `<video-stem>.<target-language>.srt` and records its path
  and SHA-256 in `run.json`.
- A job record exists under `doc/jobs/`, all referenced `doc_id` values resolve
  through `doc/index.json`, and `doc/INDEX.md` reflects the same documents.
