# Captionfy UI procedure

## Complete profile discovery

The `Subtitles contributed` grid can contain far more cards than the first rendered batch. A DOM query made near the top of the page may return only the first visible or mounted range.

1. Start at the top of the profile.
2. Move down one viewport-sized step.
3. Inspect only the cards intersecting the current viewport and record their unique YouTube IDs.
4. Compare the newly seen IDs with the authoritative YouTube playlist.
5. Repeat one step at a time. Stop only after the profile end has been observed and an additional step produces no unseen cards.

Do not use a full-page screenshot, a jump to the footer, the first 20 cards, or one bulk DOM count as evidence that later subtitles do not exist. State that a subtitle is absent only after this incremental scan covers the complete contribution grid.

When the target playlist already contains entries, derive the work queue as `authoritative YouTube order - verified Captionfy playlist membership`. Add that queue sequentially so Captionfy's append order remains correct.

## Card selection

Identify a profile card with its video link:

```css
div.rounded-lg.border.bg-card:has(a[href*="/video/youtube/VIDEO_ID"])
```

Confirm exactly one card. Its four buttons normally end with the three-dot menu; confirm the count before selecting the fourth button.

## Playlist dialog

Choose the exact menu item `Add or remove from a playlist`, then the dialog `Manage Playlists`.

Find the row whose span text exactly equals the target playlist name. Playlist rows can differ between subtitle cards, so derive the target row index from the current dialog every time. Confirm that the current row and button counts agree before clicking; do not reuse an index from a previous card. Read the row button's SVG class:

- `lucide-plus`: absent; click once.
- `lucide-loader-circle`: request pending; wait.
- `lucide-minus`: included; safe to close.

The UI can update asynchronously after a click and can temporarily rerender the row list. Poll the current dialog state until the target row returns with `lucide-minus` before closing. A fixed short delay, a missing row during rerender, or the click returning successfully is not proof of persistence.

## Final verification

On the Captionfy playlist page, collect unique IDs from links matching:

```css
a[href*="/video/youtube/"]
```

Extract the path segment after `/video/youtube/` and before `?`. Compare this ordered list with the authoritative YouTube playlist list.

Verify separately:

1. Total count.
2. Missing or extra IDs.
3. Full order when order matters.

Do not infer that `existing + newly added = expected` without comparing actual IDs.
