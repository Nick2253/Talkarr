# Talkarr — Engineering Plan

Status: **accepted direction**, 2026-10-16 (owner-confirmed model; reviewed by Codex
and Gemini, feedback folded in). Decision record for the current phase of work.

## 0. What Talkarr is

**Talkarr is a clean-up tool for subtitles that already exist.** It validates, syncs,
cleans, and flags the subtitles in a media library, whether Bazarr downloaded them or
not; it can be pointed at an existing library and fix it. **It never generates
subtitles.** Getting the existing subtitles right is how it respects artistic intent:
the forced track a film shipped with is the authority on what gets subtitled.

Priorities (owner):

1. **Forced subtitles first.** They are the subtitles nothing else handles:
   - VAD-based sync (ffsubsync, Bazarr's auto-sync) destroys them, because a few sparse
     cues get aligned against all speech in the film.
   - Downloaded forced subs are often from another release (offset, framerate), for
     another cut, or for another film.
   - Many aren't forced subs at all: **183 of ~240** `*.forced.srt` files in the library
     are > 40 KB, i.e. full subtitles mislabelled as forced (e.g. *10 Things I Hate
     About You*).
2. Regular/HI subtitles: existing ffsubsync + cleaning + validation path (done in
   Phase 0, hardened in Phase 5).
3. Bazarr integration (webhook, blacklist + re-search) is optional plumbing, last.

## 1. The core idea

Find the non-English speech in the film's audio, translate it, and use those
translations as **timed anchors** to check and re-time the existing forced subtitle.
The translations are never written to the library; they only answer "does this
forced cue belong here, and when should it appear?"

When a full English subtitle exists, a cheaper anchor source comes first: sync the full
subtitle against the audio (VAD sync works fine on dense subs), then match forced cues
to full-sub cues by text and inherit their timing. The audio path covers films without a
usable full subtitle and cross-checks the text path.

## 2. Feasibility findings (measured 2026-10-16)

Experiments (run ad hoc on eezo before the AI-only rule was stated; future runs follow
§4) on *The Hunt for Red October* against its reference forced subtitle (28 Russian
cues):

| Test | Result |
|------|--------|
| Whisper large-v3-turbo (whisper.cpp), 45 s window, `language=auto` | Detected **English 99 %** on a Russian scene; hallucinated "Shorteladriga, Devereys, Capitan". |
| Same model, per-utterance clips (2–3 s) cut at the reference cue times | Still **English 98–100 %** on 2 of 3 Russian lines. |
| Forced `language=ru` vs `en`, compare `avg_logprob` | Separates some lines, inverts others (`"Yeah!"` → `"Спасибо."` scores better). |
| SpeechBrain VoxLingua107 ECAPA (CPU), same clips | All 3 Russian clips → not English (top labels Serbian/Bosnian/Croatian: right family); clear English → English 0.998; 1.3 s "Yeah!" → garbage. |
| Full `.en.srt` vs forced `.en.forced.srt` (Red October) | Every sampled forced line appears in the full sub with near-identical wording and start times within ~10 ms. |

Conclusions:

- Whisper's language detection is unusable for this; language ID needs a dedicated
  model per speech segment, and the decision is effectively binary (English or not).
- Very short utterances need temporal context, but only as a supporting signal.
- Dialogue lives in the centre channel; extract it when the layout has one.
- The text path (full sub ↔ forced sub) is cheap and, where available, strong.
- Three clips are failure discovery, not calibration (Codex): thresholds come from the
  evaluation harness, not from these numbers.

## 3. Forced-subtitle verification & sync pipeline

```
forced sub F ─┬─▶ S0 screen (no audio) ───────────────────────────────┐
              │                                                        ▼
full sub R ───┼─▶ S1 text anchors: ffsubsync(R) → match F↔R by text ─▶ S3 fit ──▶ S4 verdict ──▶ act
              │                                                        ▲
video ────────┴─▶ S2 audio anchors: probe → extract → VAD+LID →        │
                    transcribe foreign spans → translate → match F↔A ──┘
```

**S0 Screen (cheap, no audio).** Parse F; English text?; cue count and density vs film
runtime; fraction of F cues that text-match R (F ≈ R ⇒ **mislabelled full sub**);
duplicate/overlapping cues; encoding (cleaner).

**S1 Text anchors.** Sync R with ffsubsync (record its score/offset; if it fails, R
is unusable as a timing reference). Match each F cue to R cues by normalised
fuzzy text similarity (punctuation/case/quote-style insensitive, partial matches for
split/merged cues) within a generous time window. Each match is an anchor
`(F cue time, reference time, weight)`.

**S2 Audio anchors** (when R is missing or unusable, or to cross-check):
1. *Probe*: choose the main-dialogue audio stream: exclude `commentary` /
   `visual_impaired` dispositions and commentary/description titles; prefer the
   primary-language tag, then most channels, then default; allow a per-film override.
   Record stream index and time base.
2. *Extract*: 16 kHz mono; `FC` only if the channel layout contains it, otherwise a
   downmix (bake-off: centre vs downmix recall).
3. *VAD + LID* in `talkarr-ml` (seastone, Steel; CPU): speech regions with speech probability, and
   per-region language scores from overlapping analysis windows (so a short utterance is
   classified with nearby audio without merging whole scenes). States: English /
   foreign / **uncertain** / non-speech. Gates: speech probability, LID margin/entropy.
   Scores are treated as uncalibrated until the harness calibrates them.
4. *Film-level language profile*: aggregate confident foreign regions → the film's
   foreign language(s) (usually one or two); used to constrain transcription.
5. *Transcribe* foreign/uncertain spans with whisper in the profiled language(s).
6. *Translate* to English with an LLM, batched by scene with neighbouring context, stable
   segment IDs, and an explicit "unintelligible → no translation" rule.
7. *Match* F cues to translated spans by semantic similarity within the search window;
   each match is an anchor. Log-probs, `no_speech_prob` and LLM confidence are
   supporting signals only, never hard gates; conflicting evidence lowers weight.

**S3 Fit.** Robust fit of `t_ref = a·t_F + b` over all anchors (RANSAC/Theil–Sen),
with `a` restricted to known framerate ratios (1, 25/23.976, 23.976/25, 24/23.976, …).
Outputs: `a`, `b`, inlier set, coverage = inliers / |F|, residual spread. Phase 4+: piecewise
fits for different cuts (inserted/removed scenes).

**S4 Verdict and action.**

| Verdict | Condition (thresholds from the harness) | Action |
|---------|------------------------------------------|--------|
| `in_sync` | fit ≈ identity, good coverage | leave untouched |
| `retimed` | consistent non-identity fit, good coverage | apply the transform to **every** cue (including cues with no audio, e.g. Baby Driver's sign-language scene), backup, atomic write |
| `mislabelled` | S0: F is a full subtitle | flag; optional policy (rename to `.en.srt` if no full sub exists, Bazarr re-search for a real forced sub) |
| `mismatch` | anchors exist but no consistent fit (wrong film/cut/language) | flag; optional Bazarr blacklist + re-search |
| `unverifiable` | too few anchors (no R and little foreign audio) | leave untouched, record why |
| `error` | a dependency failed (whisper/LLM/talkarr-ml down) | retry later; never confused with "no anchors" |

Cues without anchors are never evidence against a forced sub on their own: a forced
track legitimately includes signs, on-screen text and sign language.

## 4. Infrastructure and constraints

| Resource | Where | Use |
|----------|-------|-----|
| Media | Steel `/mnt/SteelVolume01/media` → `/media` in Talkarr containers | input |
| whisper.cpp large-v3-turbo | `https://llama.nkcx.net/v1/audio/transcriptions` (eezo 3060) | transcribe foreign spans |
| LLMs (gemma4-26b, qwen3.6-27b, …) | `https://llama.nkcx.net/v1/chat/completions` (eezo V100) | translation, semantic matching |
| `talkarr-ml` | seastone stack on Steel, internal `http://talkarr-ml:8000`, CPU only (4 CPUs / 4 GB cap) | VAD + LID models (Talkarr never loads models in its own container) |
| `talkarr-lab` (new) | seastone stack on Steel, compose profile `lab`, media **read-only** | eval/experiment runs via Komodo `RunStackService` |
| Gitea `NKCX/talkarr` + runner (Naquadah) | jethrik.nkcx.net | container builds/tests, `:lab` image in the Jethrik registry |
| GitHub Actions | Nick2253/Talkarr | release path only: tests on PRs, one image build per push to `main` → GHCR → production |

Constraints (owner, 2026-10-16): eezo is only for workloads that need its GPUs (no builds,
tests, media processing, experiments or CPU-only services; Talkarr reaches it only through AI
APIs). No local container runtime on the dev box: container builds/tests run on the Gitea runner.
GitHub Actions usage minimal. Pushing to `main` auto-deploys production (seastone auto-update),
so `main` only receives work that was built and smoke-tested on Gitea first.

Audio is extracted on Steel; only compressed mono audio goes to `talkarr-ml` and only
short foreign-span clips go to whisper. Generation of anchors is a batch, low-priority
workload: bounded concurrency, retries with back-off around llama-swap model swaps.

## 5. Evaluation harness

`python -m talkarr.eval run --corpus eval/corpus.yaml` (in `talkarr-lab`).

- **Corpus**: audited known-good forced subs (start: Red October, Spy Game; then the
  other ~50 small `*.forced.srt` files, each checked by hand or by a strong text match to
  its full sub). Per-film fields are independent: has audio-foreign cues, has visual
  cues, has burned-in subs, release/cut notes. Split dev/held-out **by film**
  (franchises kept together).
- **Perturbation suite** — each good forced sub is also fed in broken, with known truth:
  - shifted by ±0.5 / 2 / 10 / 60 s → expect `retimed` back to the original;
  - framerate-scaled (25/23.976, 23.976/25, 24/23.976) → `retimed`;
  - another film's forced sub → `mismatch`;
  - the film's own full sub labelled forced → `mislabelled`;
  - cues partially dropped / extra junk cues → still `retimed`/`in_sync`;
  - the untouched original → `in_sync` (**the most important case: a good sub must never be
    damaged**).
- **Metrics**: verdict confusion matrix; after-repair timing error per cue (median/p90 vs
  the original); false-retime rate on good subs (target ≈ 0); coverage; per-film
  results, not just aggregates; catastrophic failures listed explicitly; wall time
  and queue/retry time per stage.
- **Stage cache** keyed by upstream artefact hashes, audio stream, subtitle hash,
  code/model versions, prompts and decoding settings; the model identity returned by
  shared services is recorded.
- **Real-library sweep** (dry run): S0+S1 across the whole library, report verdict counts
  and a sample for owner review before anything is written.
- Cheap metrics in every run; LLM-judged checks only at milestones.

## 6. Phases

| Phase | Deliverable | Exit criteria |
|-------|-------------|---------------|
| **0 Foundation** ✅ (container check pending) | Tests (112, 94 % coverage), ruff, lean GitHub CI, multi-stage non-root image, critical file-safety fixes (Appendix A). | Image built + smoke-tested on the Gitea runner. |
| **1 Lab** | Gitea repo + runner workflow building `:lab`; `talkarr-lab` service (profile `lab`, media read-only); `RunStackService` helper script. | A trivial command runs in `talkarr-lab` and sees `/media`. |
| **2 Harness + text path** | `talkarr/forced/` (S0, S1, S3, S4), subtitle model/matching utilities, `talkarr.eval` with corpus + perturbation suite, first report; library dry-run sweep of S0. | Perturbation suite passes on films with a usable full sub; mislabel counts reviewed by owner. |
| **3 Audio path** | `talkarr-ml` (VAD+LID API, CPU, seastone on Steel); S2 (probe, extract, VAD/LID, profile, transcribe, translate, match); bake-offs (LID model, centre vs downmix, LLM). | Perturbation suite passes using audio anchors alone on the dev set; confirmed on held-out. |
| **4 Productise** | Forced-sub job in the service; library sweep mode (dry run → apply); backups + revert; dashboard verdict view; piecewise fits. | Owner signs off on a reviewed sweep. |
| **5 Integration hardening** | Appendix A remainder: Bazarr hook (correct command), optional blacklist/re-search, API auth/confinement, config validation, job recovery, docs. | Real Bazarr download processed end-to-end. |

## 6a. Progress log

**2026-10-16, Phase 0–2 (text path).**
- Phase 0 done; lab working (`scripts/lab.py` → Komodo `RunStackService` → `talkarr-lab` on
  Steel, media read-only; image built by the Gitea runner).
- Text path implemented (`talkarr/forced/`): on the real Red October subs every
  perturbation (shifts 0.5–60 s, PAL/24p scaling with and without shifts, dropped/junk
  cues, mislabel) is detected and repaired to ~1 ms.
- First library sweep (251 English forced subs, 219 with a full English sub, 59 s):
  197 mislabelled, 14 retimed, 13 in sync, 11 "mismatch", 16 unverifiable.
- What the sweep taught us (all fixed, with tests):
  - *Jaws*: a 192-cue "forced" file was a 13-minute section of a full subtitle. A naive
    fit "retimed" it by +7,487 s. Sections of full subtitles are now `mislabelled`.
  - Most "mismatches" were full subtitles that don't translate the foreign dialogue
    (John Wick 2, Atomic Blonde, Kill Bill 2, …). Missing lines are **not** evidence;
    these are `unverifiable` until the audio path exists. `mismatch` now means "lines
    found, timings inconsistent" (another cut, scrambled file).
  - Retimes are judged against an **unsynced** full subtitle unless it is synced to the
    audio first (`--sync-reference`, ffsubsync, cached). Large offsets (> 30 s),
    framerate changes, and cues mapped outside the film are flagged `review`.
- Consequence for Phase 3: of the 54 forced subs that aren't mislabelled, about half (27)
  can't be verified from text alone; that is where the audio path is needed.

**2026-10-16, synced sweep + audio path (Phase 3a).**
- Sweep with full subtitles synced to the audio first (265 forced subs, 6.6 min):
  205 mislabelled, 31 unverifiable, 15 retimed (8 flagged for review), 13 in sync,
  1 mismatch (Man on Fire: lines found, timings inconsistent; likely another cut).
- **Production bug found:** ffsubsync crashed on about 10 films because `webrtcvad` imports
  `pkg_resources` and python:3.12 images ship without setuptools. That breaks regular-sub sync in
  production too. Fixed (setuptools pinned, import smoke-tested in both CI pipelines).
- After syncing, Star Wars and Return of the Jedi want a 23.976/25 rescale: ffsubsync found
  the full sub (and so the forced sub from the same source) PAL-timed. Flagged for review;
  the audio path is the independent check.
- Audio path split in two:
  - **3a, timing:** `talkarr-ml` returns speech regions with language scores. Talkarr labels
    them, builds the foreign-speech timeline, and fits forced cues to it: the placed-cue count
    finds the neighbourhood, then overlap seconds give the exact offset and framerate.
  - **3b, content:** translation anchors (whisper + LLM) to confirm the cues *say* the
    right thing. They catch a wrong film whose timing happens to fit.
- `talkarr-ml` (Silero VAD + VoxLingua107, CPU, capped at 4 CPUs / 4 GB) is built on the Gitea
  runner. First deployed to eezo, then moved (owner: it needs no GPU) into the seastone stack on
  Steel as an internal service; the DNS request (infrastructure/dns#13) was withdrawn.

**2026-10-16, audio path on the corpus (10 films, audio alone, no full subtitle used).**
- First run (placed-count fit): 124/160. Good on films with many forced cues, but on
  films with few cues (MI3: 9, Live Free or Die Hard: 6) chance alignments against
  hundreds of partly false foreign spans produced wrong retimes, and another film's
  forced sub "fit" three films (all flagged for review, so nothing was applied blindly).
- Fixes:
  - **Chance baseline:** the sub's own cue pattern is moved far away and fitted the same
    way. A placement must beat that by at least 2 cues (10 % for larger subs), or the
    verdict is `unverifiable` ("can't tell").
  - **Exact sweep-line coarse search**, which is also over 30× faster.
  - **Overlap-seconds refinement** across scales, which recovers 24↔23.976 drift and
    sub-second shifts.
  - **Moves are justified by overlap gain:** 5 % for ≤ 2 s shifts, 20 % for bigger moves
    and framerate changes.
- Result: **127/160, 0 harmful** (no wrong retime, and no other film's forced sub accepted).
  The two tiny subs (MI3, LFODH) now say "can't tell" in all 32 cases. Of the other 128
  cases, 125 pass and 3 are conservative misses.
- `python -m talkarr.eval audio --regions DIR` replays cached analyses offline: the whole
  corpus runs in about 20 s.
- Next: translation anchors (Phase 3b). They should rescue the few-cue films and give
  content-level evidence that a subtitle belongs to the film.

**2026-10-17, production + Phase 4 start (report-only service).**
- Branch merged to GitHub `main`; GitHub CI (tests → one image build → smoke → GHCR) green;
  seastone redeployed. `talkarr-ml` moved from eezo into seastone on Steel (owner: no GPU needed).
- The service is now report-only (`DRY_RUN=true`, default): cleaning and ffsubsync run as trials
  and record what they would change. Forced subtitles get the full verification (text path
  against the synced full subtitle, audio path via talkarr-ml, combined by `forced/decide.py`).
  The verdict, review flag, reason and evidence are stored on the job and shown on the
  dashboard (filters: needs review / by verdict; `/jobs/<id>` for the evidence).
- `/process` and `/bulk` only accept paths inside `MEDIA_PATH` (review finding A1).
- Next: a small real batch (Red October, Spy Game, Star Wars, Jaws, 10 Things I Hate About
  You) reviewed with the owner before widening.

**2026-10-17, first real batch in production (report-only).** Five films, forced subs only:

| Film | Verdict | Evidence |
|------|---------|----------|
| The Hunt for Red October | in_sync | 28/28 lines match the synced full sub; audio agrees (23/28 cues on foreign speech, chance 17) |
| Spy Game | retimed | text: 24→23.976, −1.12 s (26 lines); audio alone below the chance bar, but its best fit is the same correction (0.999, −1.58 s, +84 % cue time on speech) |
| Star Wars | retimed | text: 25→23.976, −0.90 s (29/30 lines); audio: 25→24, −4.31 s, cues within 2.8 s; ffsubsync also rescaled the full sub by 0.959 |
| Jaws | mislabelled | 192 cues reproducing 80 % of the full sub within their span |
| 10 Things I Hate About You | mislabelled | 1,191 cues |

All five verdicts are right. Two framerate fixes were first flagged for review although
independent evidence supported them, so `decide.combine` now counts agreement:
- **exact:** cues within 1 s;
- **broad:** same direction, within 5 s, and a precise text fit (p90 residual ≤ 0.25 s);
- **corroborating:** an inconclusive audio fit landing on the same correction.

In these cases the review flag is cleared and the reason states the evidence.

## 7. Open questions for the owner

1. **Mislabelled "forced" files** (183 found): flag only, or also act — rename to `.en.srt`
   when no full English sub exists, delete, or ask Bazarr for a real forced sub?
2. **Re-time without review?** Once the harness shows ~0 false retimes, may `retimed`
   be applied automatically (with backup), or should the first library sweep be reviewed?
3. **Baby Driver** isn't in the library: can it be added as the sign-language test case?

---

## Appendix A — Code review findings (2026-10-16)

**Fixed in Phase 0:** E1, E2, E3, E4, E5 (credit rules limited to file edges, music cues
kept, garbage pattern removed), S1, S2, S4, P1 (tests/lint/CI), P2 (multi-stage image,
non-root, healthcheck, `.dockerignore`). Also found and fixed while writing tests: cp1252
files were decoded as cp1250 ("crème" → "crčme"); the validator could not read non-UTF-8
files; `PATH_MAPPING=/data` also matched `/database`. B2/B3: the unused, incorrect
Bazarr/Radarr/Sonarr clients were removed and will be rewritten in Phase 5.

Severity: **C** = critical (wrong behaviour / data loss in normal use),
**H** = high, **M** = medium, **L** = low/cleanup.

### Integration with Bazarr

| # | Sev | Finding |
|---|-----|---------|
| B1 | C | **The documented Bazarr hook can never work.** Bazarr ≥1.4 runs the post-processing command with `shell=False` after `shlex.split`, and only substitutes a placeholder when it is an *entire argument* (`utilities/post_processing.py::pp_replace`). The documented `-d '{"subtitle":"{{subtitles}}",...}'` sends the literal text `{{subtitles}}`. `{{movie}}` does not exist at all (`{{episode}}` is the media path for movies too). |
| B2 | C | **Blacklisting is never performed.** `worker.py` sets status `blacklisted` and posts to Discord, but never calls Bazarr. `BazarrClient` / `RadarrClient` / `SonarrClient` are dead code. |
| B3 | H | `BazarrClient` targets endpoints/params that don't match Bazarr's API: blacklist is `POST /api/movies/blacklist` (form: `radarrid, provider, subs_id, language, subtitles_path`) and `POST /api/episodes/blacklist` (`seriesid, episodeid, …`), not `/api/providers/...`; searches take query params, not JSON. Bazarr's blacklist call already deletes the file **and** triggers a re-search, so no separate delete/search is needed. `subtitles_path` must be in Bazarr's namespace (reverse path mapping). |
| B4 | H | Blacklisting needs `provider`, `subtitle_id`, and the Sonarr/Radarr ids — none are collected by the webhook. Bazarr exposes them as `{{provider}}`, `{{subtitle_id}}`, `{{series_id}}`, `{{episode_id}}` (movies: `series_id=""`, `episode_id=radarrId`). |
| B5 | M | `pp_replace` silently **drops** placeholders whose value is empty, shifting positional args. `{{series_id}}` is empty for movies, so any positional hook must put it last. |
| B6 | M | Bazarr's own auto-sync must be disabled, otherwise subs get synced twice (and forced subs destroyed by Bazarr before Talkarr sees them). Not documented. |
| B7 | L | The language placeholders never carry `:forced`/`:hi`; classification must come from the filename (Bazarr writes `*.en.forced.srt`, `*.en.hi.srt`). The `:forced` tag branch in `classifier.py` is harmless but dead for Bazarr input. |

### Cleaning / encoding

| # | Sev | Finding |
|---|-----|---------|
| E1 | C | **Mojibake repair never works.** `_fix_mojibake(raw)` does `raw.decode('latin-1').encode('latin-1').decode('utf-8')`, which is the identity on `raw`, so double-encoded UTF-8 comes back unchanged. |
| E2 | H | Mojibake patterns only match the latin-1 C1-control form (`â\x80\x99`), not the overwhelmingly common cp1252 form (`â€™`). Bare `Â`, `Ã`, `Å` are in the list → false positives on legitimate Nordic/Portuguese/French text. |
| E3 | H | Non-UTF-8 files (cp1252, latin-1…) are decoded but **not rewritten as UTF-8** unless an ad was removed, so the encoding problem survives. |
| E4 | H | Cleaning can delete **every cue** and save an empty file (no guard on removal ratio). |
| E5 | M | Ad rules are too blunt: any cue anywhere containing a URL or "subtitles by" is deleted; `r"a]]"` is garbage; punctuation-only cues (e.g. `♪`) in the first/last two positions are deleted, which removes legitimate music cues in HI subs. |
| E6 | M | `pysrt` is unmaintained and silently drops malformed cues on parse, then the rewrite loses them. `srt` (already a transitive dependency via ffsubsync) is maintained and stricter. |
| E7 | L | Non-SRT subtitles (`.ass`, `.ssa`, `.vtt`, `.sub`) are pushed through the SRT parser and fail. |

### Sync

| # | Sev | Finding |
|---|-----|---------|
| S1 | H | Framerate regex looks for `framerate ratio:`; ffsubsync 0.4.26 logs `framerate scale factor:`. Never matches. ffsubsync's own alignment `score:` is ignored. |
| S2 | H | `tempfile.mkstemp` creates the output with mode **0600**, then `shutil.move` replaces the subtitle → media servers running as another user lose read access. Original mode/group must be preserved. |
| S3 | M | No backup of the original before sync/clean, so a bad sync is irreversible. |
| S4 | M | Bulk scan pairing (`_find_media_for_subtitle`) takes `parts[0]` as the media stem (`Movie.Name.2020.en.srt` → `Movie`) then falls back to **the first video in the folder** — in a TV season folder this syncs an episode's subs against a different episode, destroying them. |

### Validation / decision logic

| # | Sev | Finding |
|---|-----|---------|
| V1 | M | `SYNC_SCORE_THRESHOLD` is never read; pass threshold is hard-coded `0.4`. `GENERATE_ENABLED`, `BASE_URL` are unused. |
| V2 | M | No "warn/flagged" terminal state: failures that don't trigger blacklist are marked `completed`, hiding them. |
| V3 | M | HI subs get no cue-count checks; an empty-after-clean file is reported as an empty download. |

### Worker / persistence

| # | Sev | Finding |
|---|-----|---------|
| W1 | H | Jobs interrupted by a restart stay in `classifying`/`syncing`/… forever (no recovery). |
| W2 | M | No atomic claim of jobs; running >1 gunicorn worker would double-process. |
| W3 | M | No schema versioning/migrations; no retention of old jobs. |
| W4 | M | Duplicate-detection ignores `classifying`; 8-char job ids. |
| W5 | L | Config is read into class attributes at import time → untestable, and a malformed float crashes import with an unhelpful error. |

### API / security

| # | Sev | Finding |
|---|-----|---------|
| A1 | H | `/process` and `/bulk` accept **any path** in the container; Talkarr will rewrite any writable `.srt` and walk any directory. Must be confined to `MEDIA_PATH` (after `realpath`). |
| A2 | M | `/bulk` walks the library synchronously inside the HTTP request (gunicorn timeout 300 s, large libraries take longer); unauthenticated even when a secret is set. |
| A3 | L | Secret compared with `!=` (use `hmac.compare_digest`). |
| A4 | M | `/health` doesn't reflect worker liveness or DB access. |

### Generation (obsolete)

`generator.py` (G1–G4) was removed in Phase 0. Talkarr does not generate subtitles;
the audio-analysis work is reframed as forced-subtitle verification and sync (§3).

### Packaging / CI / docs

| # | Sev | Finding |
|---|-----|---------|
| P1 | M | No tests, no lint, CI builds and pushes `:latest` without any checks. |
| P2 | M | Single-stage image ships gcc/python3-dev; no `USER`, no `HEALTHCHECK`, no `.dockerignore` (copies `.git`). |
| P3 | L | README documents `MEDIA_PATH=/data` (wrong) and the broken hook. |

---

### Bazarr hook (fix for B1/B4/B5, Phase 4)

Bazarr's `pp_replace` substitutes only whole-argument placeholders, and drops empty
values. Use the Python in the Bazarr image, every placeholder standalone, optional
one last:

```
python3 -c "import sys,json,urllib.request as r;k=['subtitle','media','language','provider','subtitle_id','episode_id','series_id'];r.urlopen(r.Request('http://talkarr:9494/process',json.dumps(dict(zip(k,sys.argv[1:]))).encode(),{'Content-Type':'application/json'}),timeout=10)" {{subtitles}} {{episode}} {{subtitles_language_code2}} {{provider}} {{subtitle_id}} {{episode_id}} {{series_id}}
```

`series_id` empty ⇒ movie (`episode_id` = Radarr id). Bazarr's own auto-sync must be
disabled. Blacklist via `POST /api/{movies|episodes}/blacklist` (form params), which
also deletes the file and re-searches.
