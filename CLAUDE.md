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

- **Don't use eezo** for builds, tests, media processing or experiments; it is
  reserved for AI workloads. Talkarr reaches it only via AI APIs (llama.nkcx.net
  whisper/LLMs) and the planned `talkarr-ml` model container.
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

## Architecture (current code)

```
Bazarr post-processing hook ──POST /process──▶ jobs table (SQLite) ◀── POST /bulk (library scan)
                                                   │
                                     worker thread (5 s poll)
                                                   ▼
            classify ─▶ clean ─▶ sync (ffsubsync; forced subs skipped) ─▶ validate ─▶ verdict
```

| Module | Role |
|--------|------|
| `app.py` | Flask app: dashboard, `/process`, `/bulk`, `/jobs`, `/stats`, `/health` |
| `config.py` | Env-var config (`Config` class attributes) |
| `db.py` | SQLite jobs table, `JobStatus`, `SubType` |
| `classifier.py` | forced / HI / regular from the filename (Bazarr's language placeholders never carry `:forced`) |
| `cleaner.py` | BOM, encoding (ftfy mojibake repair, cp1252 preferred on ties), ad/credit stripping with a 20 % removal guard |
| `sync.py` | ffsubsync wrapper; parses score/offset/framerate scale factor |
| `validator.py` | cue-density/duration/gap checks, mislabel detection |
| `fileio.py` | atomic, metadata-preserving writes |
| `paths.py` | `PATH_MAPPING` translation (whole path segments) |
| `notify.py` | Discord webhook |
| `worker.py` | background job loop |

Planned (see PLAN.md): `talkarr/forced/` (verification & sync pipeline), `talkarr/eval`
(harness + perturbation suite), `talkarr-ml` service.

## Bazarr integration (Phase 5, not configured yet)

Bazarr runs post-processing with `shell=False` and substitutes a placeholder only when
it is an **entire argument**, so a JSON `-d '{"subtitle":"{{subtitles}}"}'` hook sends
literal braces. Use the `python3 -c` hook in PLAN.md Appendix A. Bazarr's own auto-sync
must be disabled.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BAZARR_URL` / `BAZARR_API_KEY` | `http://bazarr:6767` / – | Bazarr API (Phase 5) |
| `RADARR_URL` / `RADARR_API_KEY` | `http://radarr:7878` / – | Radarr API (unused) |
| `SONARR_URL` / `SONARR_API_KEY` | `http://sonarr:8989` / – | Sonarr API (unused) |
| `WHISPER_URL` / `WHISPER_MODEL` | – / `whisper-large-v3-turbo` | OpenAI-compatible transcription API |
| `TALKARR_WEBHOOK_SECRET` | – | Optional Bearer token for `/process` |
| `DISCORD_WEBHOOK_URL` | – | Discord notifications |
| `DATABASE_PATH` | `/data/talkarr.db` | SQLite database |
| `BASE_URL` | `http://talkarr:9494` | External URL (unused) |
| `MEDIA_PATH` | `/media` | Media root for bulk scans |
| `SYNC_ENABLED` / `CLEAN_ENABLED` | `true` | Enable ffsubsync / cleaning |
| `GENERATE_ENABLED` | `false` | Obsolete; ignored |
| `SYNC_SCORE_THRESHOLD` | `0.4` | Unused (pass threshold is hard-coded; see Appendix A V1) |
| `PATH_MAPPING` | – | e.g. `/data:/media`; `|`-separated pairs |

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
