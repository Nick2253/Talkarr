# CLAUDE.md

## What Talkarr Is

Talkarr is a subtitle post-processor that sits alongside Bazarr in the seastone media stack. Bazarr handles provider search and download; Talkarr handles **everything else**: sync, validation, cleaning, and forced subtitle generation.

**Repo:** github.com/Nick2253/Talkarr
**Stack:** seastone on Steel (docker-compose repo at `/home/coder/projects/docker-compose/seastone/`)
**Dashboard:** talkarr.nkcx.net (behind Steel's Traefik, CNAME → seastone.nkcx.net)
**Port:** 9494
**Image:** ghcr.io/nick2253/talkarr:latest (built by GitHub Actions on push to main)

## Why Talkarr Exists

1. **Bazarr's auto-sync destroys forced subs.** ffsubsync uses VAD (Voice Activity Detection) to align subtitle timing against all speech in the audio. Forced subs are sparse — only a few cues for foreign dialogue moments. VAD sync tries to match those few cues against the full speech pattern and produces garbage. Bazarr's auto-sync is a global toggle with no way to exclude forced subs by type.

2. **Bazarr won't strip ads.** It's a "Won't happen" feature request. Users rely on third-party tools like subcleaner.

3. **No tool generates forced subs.** For primarily English-language content with scattered foreign dialogue, providers rarely have forced subs. Generating them requires per-segment language detection + selective translation — nothing chains this together today.

## Three Modes

### 1. Sync (post-download)
- **Regular/HI subs** → ffsubsync against the audio track (VAD-based, language-agnostic — works on any language audio)
- **Forced subs** → **skip sync entirely** (sparse cues would be destroyed). Future: constant-offset-only detection using embedded subs as reference
- ffsubsync runs as a subprocess, output is parsed for offset and framerate ratio
- Confidence score estimated from offset magnitude and framerate deviation

### 2. Validate + Clean (post-download)
- **Ad stripping:** regex patterns for OpenSubtitles, Subscene, and other provider watermarks
- **Encoding fix:** detect mojibake via pattern matching, attempt latin-1→UTF-8 or cp1252→UTF-8 re-decode
- **BOM stripping**
- **Validation checks:**
  - Cue density (forced: expect sparse <100 cues; regular: expect >20)
  - Duration sanity (flag cues <200ms or >30s)
  - Gap analysis (flag >10min gaps in regular subs)
  - **Mislabel detection** (200+ cues marked "forced" = probably a regular sub someone tagged wrong)
- **Action on failure:** blacklist via Bazarr API → trigger re-search → Discord notification

### 3. Generate (forced subs via Whisper) — NOT YET IMPLEMENTED
The generation pipeline is stubbed in `generator.py` but not production-ready. The plan:
1. Transcribe media with per-segment language detection via Whisper API
2. Filter segments where detected_language ≠ primary_language (from Radarr/Sonarr metadata)
3. Translate foreign segments to English via Whisper translate endpoint
4. Generate .en.forced.srt with cues only for the foreign moments

**Known challenges:**
- Whisper's per-segment language detection isn't perfect for short phrases
- Mixed-language mid-sentence dialogue is hard to segment
- Music with foreign lyrics may false-positive
- Quality won't match human translators, but infinitely better than nothing

**Infrastructure:** Whisper endpoint already available at llama.nkcx.net/v1 (whisper-large-v3-turbo) via the bazarr-openai-whisperbridge in the seastone stack. GENERATE_ENABLED=false by default.

## Architecture

```
Bazarr downloads a sub
    │
    ▼
Post-processing hook: curl POST → talkarr:9494/process
    │  payload: subtitle path, media path, language tag
    │
    ▼
Classifier → forced / regular / HI (filename patterns + Bazarr language tag)
    │
    ▼
Cleaner → ad stripping, encoding fix, BOM removal
    │
    ▼
Sync Engine → ffsubsync (regular/HI) or skip (forced)
    │
    ▼
Validator → cue density, duration, mislabel detection
    │
    ▼
Action Engine → PASS (save) / FAIL (blacklist + re-search) / WARN (flag)
```

## Tech Stack

- Python 3.12 + Flask + gunicorn (1 worker, 4 threads)
- ffsubsync (subprocess) for sync
- pysrt for SRT parsing
- charset-normalizer for encoding detection
- SQLite for job tracking
- Docker: python:3.12-slim base, needs gcc + python3-dev for webrtcvad (ffsubsync dep)

## Integration with Seastone Stack

### Path Mapping
Bazarr sends paths from its own mount namespace (e.g., `/data/movies/...`). Talkarr mounts media at `/media`. The `PATH_MAPPING=/data:/media` env var translates between them. This is handled by `talkarr/paths.py`.

### File Ownership
Runs as `user: "23611:21105"` in compose to match the media stack's PUID/PGID. The `/data` dir in the Dockerfile is chowned to this UID/GID so the named volume inherits correct permissions.

### Bazarr Post-Processing Hook (NOT YET CONFIGURED)
Needs to be set in Bazarr UI: Settings → Subtitles → Custom Post-Processing:
```
curl -s -X POST http://talkarr:9494/process -H "Content-Type: application/json" -d '{"subtitle":"{{subtitles}}","media":"{{episode}}{{movie}}","language":"{{subtitles_language}}"}'
```

### API Keys
All API keys are inline in the seastone compose file (this is a private internal Gitea repo — secrets are stored directly, no external secret management).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard (HTML) |
| GET | `/health` | Health check |
| POST | `/process` | Bazarr post-processing webhook |
| POST | `/bulk` | Trigger bulk library scan (accepts `directory` and `filter`) |
| GET | `/jobs` | Recent jobs (JSON) |
| GET | `/stats` | Job statistics (JSON) |

## Current Status

### Working
- Project skeleton: all modules written and committed
- GHCR build pipeline (GitHub Actions)
- Added to seastone compose with Traefik labels
- DNS issue filed (infrastructure/dns#12: talkarr.nkcx.net CNAME → seastone.nkcx.net)

### In Progress
- Container was crash-looping due to a SyntaxError in mojibake patterns (fixed, awaiting GHCR rebuild + redeploy)
- DNS record creation pending

### Not Yet Done
- Bazarr post-processing hook not configured (needs manual UI setup after container is healthy)
- Bulk scan mode untested
- Forced sub generation (generator.py) is stubbed but not production-ready
- No tests
- Dashboard is functional but basic — shows job list with status/score/offset

### Future Enhancements
- Forced subtitle generation via Whisper (the novel feature no other tool offers)
- Constant-offset detection for forced subs (safe alternative to full VAD sync)
- Integration with bazarr-bulk/bazarr-sync for retroactive processing of existing subs
- Subcleaner-style ad pattern database (community-maintained patterns)
- Webhook auth (TALKARR_WEBHOOK_SECRET env var is plumbed but not required)

## File Structure

```
talkarr/
├── talkarr/
│   ├── __init__.py
│   ├── app.py          # Flask app, webhook, dashboard, bulk scan
│   ├── config.py       # All config from env vars
│   ├── db.py           # SQLite: jobs table, status tracking
│   ├── classifier.py   # Forced vs regular vs HI detection
│   ├── sync.py         # ffsubsync wrapper, skips forced subs
│   ├── cleaner.py      # Ad stripping, encoding/mojibake fix
│   ├── validator.py    # Quality checks, mislabel detection
│   ├── generator.py    # Forced sub generation via Whisper (stub)
│   ├── paths.py        # Path translation between container namespaces
│   ├── bazarr.py       # Bazarr API client (blacklist, search, delete)
│   ├── radarr.py       # Radarr API client (metadata lookup)
│   ├── sonarr.py       # Sonarr API client (metadata lookup)
│   ├── notify.py       # Discord webhook notifications
│   └── worker.py       # Background job processor (5s poll loop)
├── wsgi.py             # Gunicorn entry point
├── Dockerfile
├── requirements.txt
├── .github/workflows/docker.yml
├── .gitignore
└── README.md
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BAZARR_URL` | `http://bazarr:6767` | Bazarr API URL |
| `BAZARR_API_KEY` | | Bazarr API key |
| `RADARR_URL` | `http://radarr:7878` | Radarr API URL |
| `RADARR_API_KEY` | | Radarr API key |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr API URL |
| `SONARR_API_KEY` | | Sonarr API key |
| `WHISPER_URL` | | OpenAI-compatible Whisper API endpoint |
| `WHISPER_MODEL` | `whisper-large-v3-turbo` | Whisper model name |
| `TALKARR_WEBHOOK_SECRET` | | Optional Bearer token for webhook auth |
| `DISCORD_WEBHOOK_URL` | | Discord webhook for notifications |
| `DATABASE_PATH` | `/data/talkarr.db` | SQLite database path |
| `BASE_URL` | `http://talkarr:9494` | External URL for dashboard |
| `MEDIA_PATH` | `/media` | Media root for bulk scans |
| `SYNC_ENABLED` | `true` | Enable ffsubsync for regular/HI subs |
| `CLEAN_ENABLED` | `true` | Enable ad/encoding cleaning |
| `GENERATE_ENABLED` | `false` | Enable forced sub generation via Whisper |
| `SYNC_SCORE_THRESHOLD` | `0.4` | Minimum validation score to pass |
| `PATH_MAPPING` | | Path translation (e.g., `/data:/media`) |

## Related Context

- **Seastone stack:** `/home/coder/projects/docker-compose/seastone/docker-compose.yaml` — the *arr media automation stack
- **Resolverr:** github.com/Nick2253/resolverr — sibling project that auto-resolves Seerr issue reports (routes to Radarr/Sonarr for video/audio, Bazarr for subtitles). Same Flask + SQLite + GHCR pattern.
- **Bazarr whisper bridge:** `bazarr-openai-whisperbridge` service in seastone, proxies to llama.nkcx.net/v1
- **User's subtitle priorities:** en:forced is most important personally (foreign dialogue in English content); regular en subs important for family member who watches at low volume; user does NOT want English subs for English-language content
- **Tdarr flow:** handles video/audio encoding. Subtitle work is Bazarr + Talkarr's domain.
- **Homelab domain:** nkcx.net. All internal services follow `{name}.nkcx.net`. Secrets stored inline (private Gitea repo is the security boundary).
