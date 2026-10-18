# Talkarr interface system

**Direction: the subtitle QC bay.** A darkened edit bay where someone checks subtitle
tracks against picture and sound. Calm, precise, dense enough to scan a queue, and
quiet everywhere except where action is needed. Dark only (owner decision). Tokens and
components live in `talkarr/static/talkarr.css`.

## Colour (one accent)

| Token | Value | Meaning |
|-------|-------|---------|
| `--bay` | `#15161a` | the room (page) |
| `--desk` / `--desk-raised` | `#1b1c21` / `#222329` | panels / selected rows, +3% steps |
| `--inset` | `#111215` | inputs (recessed, darker than the panel) |
| `--rule` / `--rule-strong` | white 7% / 13% | hairlines |
| `--slate` → `--slate-4` | `#e9e6df` … `#57564f` | four text levels: primary, supporting, metadata, disabled |
| `--subtitle` | `#f2cf4a` | **the only accent**: burnt-in subtitle yellow = something for you to act on (primary buttons, counts, proposed values) |
| `--waveform` | `#63c49a` | verified / in sync / applied / safe tier |
| `--tally` | `#e5534b` | live (Full Auto lamp) / wrong / destructive / uncertain tier |
| `--neutral` | `#8a8f98` | can't tell |

Rules: never spend yellow on decoration (the subtitle-type chips are neutral). Semantic
colours (`waveform`, `tally`, `neutral`) are separate from the accent.

## Type

- UI: **Archivo** (variable, bundled), 14px base. Labels and eyebrows use
  `font-stretch: 85%`, 11px, 600, `letter-spacing: .12em`, uppercase.
- Data: **IBM Plex Mono** (bundled) with `tabular-nums` for every timecode, offset, path
  and fix summary. Timecodes are `HH:MM:SS,mmm` (SRT style).
- Scale ≈ 1.25: 11 · 12 · 13.5 · 14 · 15 (section/panel titles, 600) · 24–26 (page/job
  titles, 700, stretch 90%) · 30 (review headline, 700, stretch 95%).
- Hierarchy through weight and the four text levels before size.

## Depth and spacing

- Depth: surface shifts plus 1px rings (`box-shadow: 0 0 0 1px var(--rule)`), no drop
  shadows. Radius: 4px controls, 8px panels, pills for the tally lamp and filter chips.
- Base unit 4px. Rows: 16px vertical padding with hairline separators. Panels: 20px
  padding. Section gaps 40px. Page gutter `max(16px, safe-area)`.

## Signature elements

- **Tally lamp** (top bar, links to Settings): grey dot = Manual, yellow = Safe Auto,
  red with halo = Full Auto ("live").
- **Cue ribbon** (job page, `talkarr.js`): lanes NOW / AFTER / SPEECH over the film's
  runtime; a connector from each cue's position now to after the fix; after-ticks
  yellow when they land on foreign speech, red when off it. Redraws live from the Adjust
  panel.
- **Fix lines**: mono summaries in timecode terms ("Retime 25 → 23.976 fps, −0.90 s")
  with a tier dot (green safe, yellow confident, red uncertain).

## Components

- Queue row: grid `1.15fr · 1.35fr · auto` (subtitle · fixes + plain reason · actions);
  stacks below 900px.
- Buttons: 36px min height, 13px/600, 4px radius. `btn-apply` (yellow), `btn-quiet`
  (ring), `btn-ghost` (text), `btn-danger`. Press `scale(.97)`. Destructive actions use
  `data-confirm` (two clicks; the armed state turns red and says what will happen).
- Verdict badge: 10px/600 uppercase on a 10–14% wash of its semantic colour.
- Kind chip: neutral ring, uppercase 10px.
- Mode switch: radio cards; checked = yellow ring + wash (Full Auto: red).
- Spec sheet: label + env name (mono, disabled colour) · value (mono) · source badge
  (`compose` in green, `default` neutral).
- Phone (<900px): job page panels reorder to fixes → adjust → ribbon → evidence → history
  (`display: contents` on the two column stacks plus `order`).

## Copy

Plain, from the user's side: "Waiting for you", "Restore the original", "Can't verify".
Reasons in the queue are one plain sentence (`ui.plain_reason`); the technical reason
(scales, offsets, chance counts) lives in Evidence on the job page.
