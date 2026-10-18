# Talkarr

Subtitle clean-up tool for an existing media library, designed to sit alongside Bazarr (but not require it). It validates, syncs, and cleans the subtitles you already have, with a focus on the ones nothing else handles well: **forced subtitles**. Talkarr never generates subtitles.

## What it does

- **Sync** — Aligns subtitle timing to audio via ffsubsync. Skips forced subs (sparse cues would be destroyed by VAD-based sync).
- **Clean** — Strips ad watermarks, fixes encoding/mojibake, removes BOM.
- **Validate** — Checks cue density, duration sanity, mislabel detection (200-cue "forced" sub = mislabeled regular sub). Blacklists bad subs via Bazarr API and triggers re-search.
- **Forced-subtitle verification & sync** *(in development)*: detects the non-English speech in the audio, translates it, and uses it as timed anchors to check that a forced subtitle belongs to this release, and to re-time it. It also detects full subtitles mislabelled as forced. See [docs/PLAN.md](docs/PLAN.md).

## Web UI

Talkarr's dashboard (port 9494) is where you review its work:

- **Review**: the fixes waiting for you, each stated plainly ("Retime 25 → 23.976 fps,
  −0.90 s"), with Apply, Open and Dismiss.
- **Job page**: the evidence, the *cue ribbon* (where each cue sits now, after the fix,
  and where the foreign speech is), an Adjust panel to tweak a retime before applying,
  and Restore for anything already changed.
- **Library**: scan folders you already have, and browse every checked subtitle.
- **Activity**: what was applied or dismissed, with Restore.
- **Settings**: the mode, plus a read-only view of the compose configuration.

### Modes

| Mode | What Talkarr does on its own |
|------|------------------------------|
| Manual | Nothing. It proposes fixes; you apply them. |
| Safe Auto | Applies corroborated, reversible fixes: cleaning, and forced retimes both checks agree on. |
| Full Auto | Applies every fix it is confident about, including syncs and handling mislabelled files per `MISLABELLED_ACTION`. |

Uncertain fixes always wait for you. Every change backs up the original first.

## How it integrates

Bazarr downloads subtitles from providers; its Custom Post-Processing can notify Talkarr about each new subtitle (`POST /process`). Library-wide clean-up runs through `POST /bulk`.

> **Note:** the Bazarr hook command needs care: Bazarr only substitutes placeholders that are a whole argument, so the usual `curl -d '{"subtitle":"{{subtitles}}"}'` form sends literal braces. See [docs/PLAN.md](docs/PLAN.md), Appendix A, for a working command. Disable Bazarr's own auto-sync, or it will destroy forced subs before Talkarr sees them.

## Development

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/python -m pytest --cov
```

## Configuration

Set everything in the compose file; the Settings page shows it read-only. The mode can be
changed on the Settings page. The full list, with defaults, is in
[CLAUDE.md](CLAUDE.md#environment-variables). The ones you will usually set:

| Variable | Example | |
|----------|---------|---|
| `MEDIA_PATH` | `/media` | Media root inside the container |
| `PATH_MAPPING` | `/data:/media` | Bazarr's paths → Talkarr's |
| `TALKARR_MODE` | `manual` | Starting mode |
| `MISLABELLED_ACTION` | `remove_and_backup` | `remove_and_backup` (e.g. let Bazarr fetch a real forced track), `rename_or_remove_and_backup`, or `flag`. The original is backed up first either way |
| `TALKARR_ML_URL` | `http://talkarr-ml:8000` | Speech analysis for the forced-subtitle audio check |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard |
| `GET` | `/health` | Health check |
| `POST` | `/process` | Bazarr post-processing webhook |
| `POST` | `/bulk` | Queue a library scan: `{"directory": "/media/movies/Film (Year)", "filter": "forced", "requeue": false}` |
| `GET` | `/jobs` | Recent jobs (JSON); `?verdict=retimed&review=1` |
| `GET` | `/jobs/<id>` | One job with its full verification report |
| `GET` | `/stats` | Job statistics (JSON) |

## Docker

```yaml
talkarr:
  image: ghcr.io/nick2253/talkarr:latest
  container_name: talkarr
  restart: unless-stopped
  environment:
    - BAZARR_URL=http://bazarr:6767
    - BAZARR_API_KEY=your-key
  volumes:
    - talkarr-data:/data
    - /path/to/media:/media
  expose:
    - 9494
```
