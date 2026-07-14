---
name: captionfy-bulk-workflow
description: Upload local subtitle files to Captionfy through the captionfy-bulk-uploader loopback API, monitor queue completion, and add uploaded subtitles to Captionfy playlists in Chrome. Use for Captionfy bulk uploads, local bridge API jobs, playlist membership, YouTube-to-Captionfy playlist reconciliation, missing subtitle detection, and final count or order verification.
---

# Captionfy bulk workflow

Use the local bridge API for uploads and Chrome only for Captionfy UI operations.

## Upload subtitles

1. Locate `captionfy_bridge.py`; prefer the installed uploader source, commonly `D:\Playground\chrome-extension\captionfy-bulk-uploader\captionfy_bridge.py`.
2. Check `http://127.0.0.1:8765/health` with the user-provided bearer token.
3. Match every subtitle to its exact 11-character YouTube ID before enqueueing.
4. Pass the original subtitle path directly to `enqueue`. Do not edit, rename, move, copy, or delete source files. Do not use a browser file chooser when the bridge is available.
5. Set `PYTHONIOENCODING=utf-8` before CLI calls so emoji and punctuation in filenames do not cause CP949 output failures.
6. Default to Korean, finished, public, credited, and non-collaborative unless the user requests otherwise.
7. Poll `status` until every new job is `completed` or `failed`. An enqueue command may print an encoding error after the server already accepted the job; inspect `status` before retrying to avoid duplicates.

Read [references/bridge-api.md](references/bridge-api.md) for commands and status handling.

## Manage Captionfy playlists

Use `chrome:control-chrome` and follow its skill instructions.

1. Reuse one existing signed-in Captionfy tab. Do not repeatedly create, close, or replace tabs.
2. Open the profile and scan `Subtitles contributed` incrementally. Never treat the first rendered card batch as the full profile.
3. Scroll one viewport step, inspect the cards currently on screen, and accumulate unique YouTube IDs from their `href`. Repeat until the profile has been covered and a further step yields no unseen cards. Do not use a single full-page jump or one initial DOM count as proof of completeness.
4. Compare the accumulated profile IDs with the authoritative YouTube playlist before claiming that any subtitle is absent.
5. Process missing playlist members one card at a time in YouTube order. On each card, open the three-dot menu and choose `Add or remove from a playlist`.
6. Select the exact playlist. Treat a minus icon as included, a plus icon as absent, and a spinning loader as pending.
7. After clicking plus, poll until the loader becomes minus. Close the dialog only after persistence is confirmed, then move to the next card.
8. Navigate the same tab to the playlist page and extract unique YouTube IDs from subtitle links.
9. Compare against the authoritative YouTube playlist IDs. Report count, missing IDs, extra IDs, and full order. Do not infer completeness from an earlier visible range.
10. Preserve the final playlist tab as the deliverable.

Read [references/captionfy-ui.md](references/captionfy-ui.md) for stable selectors and verification rules.

## Ordering

Captionfy appends newly added subtitles. If the user requires YouTube order, verify the entire ID sequence, not only membership or total count. Do not claim order is correct unless the full sequences match. If Captionfy offers no supported reorder control, state that clearly instead of treating a correct count as correct ordering.

## Safety

- Never expose or store bridge tokens in the skill.
- Never delete or rewrite source subtitles during upload or playlist work.
- Avoid duplicate uploads: query bridge status and the Captionfy profile first.
- Keep browser interactions scoped to the user-authorized Captionfy account and playlist.
