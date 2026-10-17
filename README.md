# Talkarr

Subtitle clean-up tool for an existing media library, designed to sit alongside Bazarr (but not require it). It validates, syncs, and cleans the subtitles you already have, with a focus on the ones nothing else handles well: **forced subtitles**. Talkarr never generates subtitles.

## What it does

- **Sync** — Aligns subtitle timing to audio via ffsubsync. Skips forced subs (sparse cues would be destroyed by VAD-based sync).
- **Clean** — Strips ad watermarks, fixes encoding/mojibake, removes BOM.
- **Validate** — Checks cue density, duration sanity, mislabel detection (200-cue "forced" sub = mislabeled regular sub). Blacklists bad subs via Bazarr API and triggers re-search.
- **Forced-subtitle verification & sync** *(in development)*: detects the non-English speech in the audio, translates it, and uses it as timed anchors to check that a forced subtitle belongs to this release, and to re-time it. It also detects full subtitles mislabelled as forced. See [docs/PLAN.md](docs/PLAN.md).

## How it integrates

Bazarr downloads subtitles from providers; its Custom Post-Processing can notify Talkarr about each new subtitle (`POST /process`). Library-wide clean-up runs through `POST /bulk`.

> **Note:** the Bazarr hook command needs care: Bazarr only substitutes placeholders that are a whole argument, so the usual `curl -d '{"subtitle":"{{subtitles}}"}'` form sends literal braces. See [docs/PLAN.md](docs/PLAN.md), Appendix A, for a working command. Disable Bazarr's own auto-sync, or it will destroy forced subs before Talkarr sees them.

## Development

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
.venv/bin/ruff check . && .venv/bin/python -m pytest --cov
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BAZARR_URL` | `http://bazarr:6767` | Bazarr API URL |
| `BAZARR_API_KEY` | | Bazarr API key |
| `RADARR_URL` | `http://radarr:7878` | Radarr API URL |
| `RADARR_API_KEY` | | Radarr API key |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr API URL |
| `SONARR_API_KEY` | | Sonarr API key |
| `WHISPER_URL` | | OpenAI-compatible Whisper API (for forced sub generation) |
| `WHISPER_MODEL` | `whisper-large-v3-turbo` | Whisper model name |
| `TALKARR_WEBHOOK_SECRET` | | Optional Bearer token for webhook auth |
| `DISCORD_WEBHOOK_URL` | | Discord webhook for notifications |
| `DATABASE_PATH` | `/data/talkarr.db` | SQLite database path |
| `BASE_URL` | `http://talkarr:9494` | External URL for dashboard links |
| `MEDIA_PATH` | `/media` | Media root for bulk scans |
| `SYNC_ENABLED` | `true` | Enable ffsubsync |
| `CLEAN_ENABLED` | `true` | Enable ad/encoding cleaning |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard |
| `GET` | `/health` | Health check |
| `POST` | `/process` | Bazarr post-processing webhook |
| `POST` | `/bulk` | Trigger bulk library scan |
| `GET` | `/jobs` | Recent jobs (JSON) |
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
