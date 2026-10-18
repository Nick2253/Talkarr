# CLAUDE.md

## What Talkarr is

Talkarr is a **clean-up tool for subtitles that already exist** in the seastone media
library. It validates, syncs, cleans and flags them, whether Bazarr downloaded them or
not, and can be pointed at an existing library to fix it. **It never generates
subtitles.** Don't propose generating, finding or machine-translating subtitles as
output; the forced track a film shipped with is the authority on what gets subtitled.

The hard, unsolved part, and the current priority, is **forced subtitles**:
- VAD sync (ffsubsync, Bazarr's auto-sync) destroys them: a few sparse cues get aligned
  against all speech in the film.
- Downloaded forced subs are often from another release (offset/framerate), another cut,
  or another film.
- 183 of ~240 `*.forced.srt` files in the library are full subtitles mislabelled as
  forced.

The approach: find the non-English speech in the audio, translate it, and use those
translations as timed **anchors** to verify and re-time the existing forced sub.
Translations are never written out. When a full English sub exists, a cheaper text
path comes first: sync the full sub with ffsubsync, then match forced cues to it by text.

**`docs/PLAN.md` is the source of truth for design, phases, and open questions. Read it
first.** Appendix A lists the code-review findings and which are fixed.

- **Repo:** github.com/Nick2253/Talkarr (public). Gitea `NKCX/talkarr` is used for lab
  builds only.
- **Production:** `talkarr` service in the seastone stack on Steel
  (`/home/coder/projects/docker-compose/seastone/`), image `ghcr.io/nick2253/talkarr:latest`,
  dashboard https://talkarr.nkcx.net, port 9494.
- **Pushing to `main` deploys to production**: GitHub Actions builds `:latest`, and
  seastone auto-updates. Only merge work that has been built and smoke-tested first.

## Working rules (from the owner)

- **eezo is only for workloads that need its GPUs.** No builds, tests, media processing,
  experiments or CPU-only services there. Talkarr reaches it only through AI APIs
  (llama.nkcx.net whisper/LLMs). `talkarr-ml` is CPU-only and runs in seastone on Steel.
- The dev box has no container runtime. Container builds and tests run on the Gitea
  runner (Naquadah) or on Veridium.
- Keep GitHub Actions usage minimal (tests on PRs, one image build per push to `main`).
- Experiments and eval runs go in the `talkarr-lab` container on Steel (read-only
  media), started through Komodo `RunStackService`.

## Development

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m pytest --cov
```

- Tests live in `tests/`. `conftest.py` gives every test an isolated SQLite DB and
  default config (via `monkeypatch` on `Config`) and provides `make_srt()` and
  `srt_cue()` builders. Write tests against generated SRT files in `tmp_path`, never
  real media.
- External processes and services are faked in tests: `subprocess.run` for ffsubsync,
  `responses` for HTTP.
- Every write to the library goes through `talkarr/fileio.py`. It is atomic and
  preserves the original file's mode and owner.

## Lab (experiments against the real library)

```bash
git push gitea <branch>          # Gitea runner (.gitea/workflows/lab.yml): tests, build,
                                 # smoke test, push jethrik.nkcx.net/nkcx/talkarr:lab
python3 scripts/lab.py python -m talkarr.eval sweep --sync-reference
python3 scripts/lab.py --no-pull ls /media/movies      # skip the image pull
python3 scripts/lab.py --user 0 sh -c "..."            # one-off maintenance as root
```

`scripts/lab.py` runs a command in the `talkarr-lab` service (stack `seastone-lab` on
Steel, defined in the docker-compose repo) through Komodo `RunStackService`. Output comes
back when the container exits. Media is mounted read-only; results and caches go to the
`/lab` volume (`/lab/runs/*.json`, `/lab/cache/`). Runs are niced; Steel's CPU is shared
with backups, so avoid needless full-library ffsubsync passes (the reference-sync cache
makes reruns cheap).

## Architecture (current code)

```
Bazarr hook ──POST /process──▶ jobs (SQLite) ◀── POST /bulk, or "Start scan" in the UI
                                    │
                         worker thread (5 s poll)
                                    ▼
   classify ─▶ analyse (never writes): cleaning trial, ffsubsync trial, forced verification
          ─▶ proposal: fixes with a tier (safe / confident / uncertain) + file fingerprint
          ─▶ mode decides what is applied now: Manual none · Safe Auto safe · Full Auto safe+confident
                                    ▼
   web UI (Review / Library / Activity / Settings): apply, adjust, dismiss, restore, re-check
```

Every applied fix backs up the original first (`BACKUP_DIR/<job>/<time>/`), and every
change can be restored from the UI. Applying refuses if the subtitle changed on disk
since it was checked. The mode is the only setting changed in the UI (stored in the
`settings` table); everything else is environment (compose), shown read-only.

| Module | Role |
|--------|------|
| `app.py` | Flask app + machine API: `/process` (Bazarr), `/bulk`, `/jobs`, `/jobs/<id>` (JSON), `/stats`, `/health`; `queue_scan`; paths confined to `MEDIA_PATH` |
| `ui.py` | Web UI blueprint: pages, form actions, same-origin check on POSTs, plain-language reasons |
| `templates/`, `static/` | Jinja templates; `talkarr.css` (design tokens, see `.interface-design/system.md`), `talkarr.js` (progressive enhancement: cue ribbon, adjust preview, two-click confirms), bundled fonts |
| `settings.py` | Modes (`manual`, `safe_auto`, `full_auto`), the read-only configuration list |
| `fixes.py` | Building proposals, tiers, auto-apply policy, applying with backups, reverting |
| `review.py` | Apply / dismiss / revert / re-check (shared by the UI and the automatic modes) |
| `bazarr.py` | Finds Bazarr's download record for a subtitle (movie or episode history) and blacklists it, so a removed subtitle isn't fetched again |
| `scanner.py` | Background library scans (one at a time) with progress for the Library page |
| `worker.py` | Job loop: analyse → propose → auto-apply per mode |
| `config.py` | Env-var config (`Config` class attributes) |
| `db.py` | SQLite: jobs (verdict, review, report, proposal, outcome, backup), settings |
| `classifier.py` | forced / HI / regular from the filename |
| `cleaner.py` | BOM, encoding (ftfy, cp1252 preferred on ties), ad/credit stripping (20 % guard); `write=False` for trials |
| `mods.py` | Bazarr's subtitle modifications (own rules, same names), proposed as a safe and a confident fix; `remove_HI` skips HI subtitles |
| `sync.py` | ffsubsync wrapper (`run_ffsubsync` into any output path; `sync_subtitle` in place) |
| `validator.py` | Cue-density/duration/gap checks |
| `fileio.py` | Atomic, metadata-preserving writes |
| `media.py` | Subtitle → video pairing; English forced subtitle names; full-subtitle sibling |
| `forced/` | Forced verification: `subtitles`, `anchors` + `fit` (text path), `audio` (timeline path), `verify` (verdicts), `decide` (combine), `sources` (cached evidence), `service` (one file end to end, incl. ribbon data) |
| `eval/` | Harness: `sweep`, `perturb`, `audio` (`--regions DIR` replays cached analyses offline) |
| `ml/` | The `talkarr-ml` service (Silero VAD + VoxLingua107, CPU), its own image |

To look at the UI locally: seed a scratch database and media folder, run the Flask app
with `MEDIA_PATH`/`DATABASE_PATH` pointing at them, and screenshot with Playwright's
headless Chromium (the Freshell browser can't reach this container). Flask caches
templates: restart the server after editing them.

## Bazarr integration (Phase 5, not configured yet)

Bazarr runs post-processing with `shell=False` and substitutes a placeholder only when
it is an **entire argument**, so a JSON `-d '{"subtitle":"{{subtitles}}"}'` hook sends
literal braces. Use the `python3 -c` hook in PLAN.md Appendix A. Bazarr's own auto-sync
must be disabled.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDIA_PATH` | `/media` | Media root; Talkarr only reads and changes files inside it |
| `PATH_MAPPING` | – | e.g. `/data:/media`; `|`-separated pairs |
| `TALKARR_MODE` | `manual` | Mode at first start: `manual`, `safe_auto`, `full_auto` (the Settings page overrides it) |
| `MISLABELLED_ACTION` | `rename_or_remove_and_backup` | Full subtitle saved as forced: `remove` (no backup), `remove_and_backup`, `rename_or_remove_and_backup`, `flag` |
| `BAZARR_BLACKLIST` | `true` | With `BAZARR_API_KEY` set: removing a subtitle Bazarr downloaded blacklists it in Bazarr (Bazarr deletes it and searches for a different one) instead of deleting the file directly |
| `BACKUP_DIR` | `/data/backups` | Originals are copied here before any change |
| `SYNC_MIN_OFFSET` | `0.15` | Smallest ffsubsync correction worth proposing (s) |
| `SYNC_MIN_CONFIDENCE` | `0.8` | ffsubsync confidence at which a sync counts as confident (Full Auto) |
| `CLEAN_ENABLED` / `SYNC_ENABLED` / `FORCED_VERIFY_ENABLED` | `true` | Which checks run |
| `SUBTITLE_MODS` | – | Bazarr's text modifications, by Bazarr's names: `common`, `remove_tags`, `OCR_fixes`, `remove_HI`, `fix_uppercase`, `reverse_rtl`, `emoji`, `color(name=…)` |
| `TALKARR_ML_URL` | – | talkarr-ml (seastone: `http://talkarr-ml:8000`); empty disables the audio check |
| `CACHE_DIR` | `/data/cache` | Synced references, audio analyses, trial-sync scratch |
| `DATABASE_PATH` | `/data/talkarr.db` | SQLite database |
| `TALKARR_WEBHOOK_SECRET` | – | When set, `/process` and `/bulk` need `Authorization: Bearer <secret>` |
| `TALKARR_SECRET_KEY` | random | Signs the flash-message cookie |
| `BAZARR_URL` / `BAZARR_API_KEY` | `http://bazarr:6767` / – | Bazarr API (`bazarr.py`: history lookup + blacklist) |
| `DISCORD_WEBHOOK_URL` | – | Discord notifications |
| `WHISPER_URL` / `WHISPER_MODEL` | – / `whisper-large-v3-turbo` | Transcription API (Phase 3b) |
| `RADARR_*`, `SONARR_*`, `BASE_URL`, `SYNC_SCORE_THRESHOLD` | | Unused |

## Related context

- Resolverr (github.com/Nick2253/resolverr): sibling project using the same Flask +
  SQLite + GHCR pattern.
- Owner's subtitle priorities: en:forced matters most personally (foreign dialogue in
  English content). Regular en subs matter for a family member who watches at low
  volume. No English subs wanted for English-language content.
- The community forced-subtitle sheet (which films have forced, separate or burned-in
  subs) is Google Sheet `1nAMVVnmk3vSaUiuFXcN7D0R69FHIP4tfIkTdThTkXEk`.
- Test films in the library: The Hunt for Red October and Spy Game (good small forced
  subs); 10 Things I Hate About You (mislabelled forced); The Accountant (no forced
  sidecar). Baby Driver (sign-language forced cues) is not in the library yet.
