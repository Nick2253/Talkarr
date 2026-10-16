# Talkarr

Subtitle post-processor that sits alongside Bazarr. Handles everything Bazarr doesn't: sync, validation, cleaning, and forced subtitle generation.

## What it does

- **Sync** — Aligns subtitle timing to audio via ffsubsync. Skips forced subs (sparse cues would be destroyed by VAD-based sync).
- **Clean** — Strips ad watermarks, fixes encoding/mojibake, removes BOM.
- **Validate** — Checks cue density, duration sanity, mislabel detection (200-cue "forced" sub = mislabeled regular sub). Blacklists bad subs via Bazarr API and triggers re-search.
- **Generate** — Creates forced subtitles for foreign-language dialogue using Whisper (language detection → translation → SRT). Requires a Whisper-compatible API endpoint.

## How it integrates

Bazarr downloads subtitles from providers. After each download, Bazarr's Custom Post-Processing sends a webhook to Talkarr:

```
curl -s -X POST http://talkarr:9494/process \
  -H "Content-Type: application/json" \
  -d '{"subtitle":"{{subtitles}}","media":"{{episode}}{{movie}}","language":"{{subtitles_language}}"}'
```

Talkarr classifies, cleans, syncs, and validates the subtitle. Failed subs are blacklisted and re-searched automatically.

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
| `MEDIA_PATH` | `/data` | Media root for bulk scans |
| `SYNC_ENABLED` | `true` | Enable ffsubsync |
| `CLEAN_ENABLED` | `true` | Enable ad/encoding cleaning |
| `GENERATE_ENABLED` | `false` | Enable forced sub generation |

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
