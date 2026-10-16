import json
import logging
import os
import uuid

from flask import Flask, request, jsonify, render_template_string

from .config import Config
from .db import (init_db, create_job, get_job, get_recent_jobs, get_stats,
                 get_job_by_subtitle, JobStatus)
from .worker import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

DASHBOARD_PAGE = """<!doctype html>
<html>
<head>
<title>Talkarr</title>
<style>
  :root { --bg: #0f0f1a; --card: #1a1a2e; --accent: #16213e;
          --border: #2a2a4a; --text: #e0e0e0; --dim: #888;
          --green: #2ecc71; --red: #e74c3c; --yellow: #f1c40f;
          --blue: #3498db; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif;
         background: var(--bg); color: var(--text); padding: 2rem; }
  h1 { font-size: 1.6rem; margin-bottom: 1.5rem; }
  .stats { display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }
  .stat { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 1rem 1.5rem; min-width: 120px; }
  .stat-value { font-size: 1.8rem; font-weight: 700; }
  .stat-label { color: var(--dim); font-size: 0.8rem; text-transform: uppercase;
                letter-spacing: 0.05em; margin-top: 0.2rem; }
  table { width: 100%; border-collapse: collapse; background: var(--card);
          border-radius: 8px; overflow: hidden; }
  th, td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid var(--border); }
  th { background: var(--accent); color: var(--dim); font-size: 0.75rem;
       text-transform: uppercase; letter-spacing: 0.05em; }
  td { font-size: 0.85rem; }
  .status { padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.75rem;
            font-weight: 600; display: inline-block; }
  .status-completed { background: rgba(46,204,113,0.15); color: var(--green); }
  .status-failed, .status-blacklisted { background: rgba(231,76,60,0.15); color: var(--red); }
  .status-pending, .status-classifying, .status-syncing, .status-cleaning,
  .status-validating { background: rgba(52,152,219,0.15); color: var(--blue); }
  .status-skipped { background: rgba(241,196,15,0.15); color: var(--yellow); }
  .type { padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.7rem;
          font-weight: 600; text-transform: uppercase; }
  .type-forced { background: rgba(241,196,15,0.15); color: var(--yellow); }
  .type-regular { background: rgba(52,152,219,0.15); color: var(--blue); }
  .type-hi { background: rgba(155,89,182,0.15); color: #9b59b6; }
  .path { max-width: 400px; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; font-family: monospace; font-size: 0.8rem; }
  .score { font-weight: 600; }
  .score-good { color: var(--green); }
  .score-warn { color: var(--yellow); }
  .score-bad { color: var(--red); }
  .empty { text-align: center; padding: 3rem; color: var(--dim); }
</style>
</head>
<body>
  <h1>Talkarr</h1>
  <div class="stats">
    <div class="stat">
      <div class="stat-value">{{ stats.get('total', 0) }}</div>
      <div class="stat-label">Total</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color: var(--green)">{{ stats.get('completed', 0) }}</div>
      <div class="stat-label">Completed</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color: var(--red)">{{ stats.get('failed', 0) + stats.get('blacklisted', 0) }}</div>
      <div class="stat-label">Failed</div>
    </div>
    <div class="stat">
      <div class="stat-value" style="color: var(--blue)">{{ stats.get('pending', 0) }}</div>
      <div class="stat-label">Pending</div>
    </div>
  </div>
  {% if jobs %}
  <table>
    <thead>
      <tr>
        <th>File</th>
        <th>Type</th>
        <th>Status</th>
        <th>Score</th>
        <th>Sync Offset</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
    {% for job in jobs %}
      <tr>
        <td class="path" title="{{ job.subtitle_path }}">{{ job.subtitle_path.split('/')[-1] if job.subtitle_path else 'Unknown' }}</td>
        <td><span class="type type-{{ job.sub_type or 'unknown' }}">{{ job.sub_type or '?' }}</span></td>
        <td><span class="status status-{{ job.status }}">{{ job.status }}</span></td>
        <td>
          {% if job.validation_score is not none %}
            <span class="score {% if job.validation_score >= 0.7 %}score-good{% elif job.validation_score >= 0.4 %}score-warn{% else %}score-bad{% endif %}">
              {{ "%.0f"|format(job.validation_score * 100) }}%
            </span>
          {% else %}-{% endif %}
        </td>
        <td>{{ "%.2fs"|format(job.sync_offset) if job.sync_offset is not none else '-' }}</td>
        <td style="font-size: 0.75rem; color: var(--dim); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
            title="{{ job.actions_taken or '' }}">
          {{ job.actions_taken or '-' }}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="empty">No jobs yet. Waiting for Bazarr to send subtitles.</div>
  {% endif %}
</body>
</html>
"""


@app.before_request
def _check_webhook_secret():
    if request.path == "/process" and Config.WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {Config.WEBHOOK_SECRET}":
            return jsonify({"error": "unauthorized"}), 401


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def dashboard():
    stats = get_stats()
    jobs = get_recent_jobs(limit=100)
    return render_template_string(DASHBOARD_PAGE, stats=stats, jobs=jobs)


@app.route("/process", methods=["POST"])
def process():
    """Webhook endpoint for Bazarr post-processing."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "no payload"}), 400

    subtitle_path = payload.get("subtitle")
    media_path = payload.get("media")
    language = payload.get("language", "")

    if not subtitle_path:
        return jsonify({"error": "missing subtitle path"}), 400

    existing = get_job_by_subtitle(subtitle_path)
    if existing and existing["status"] in (JobStatus.PENDING, JobStatus.SYNCING,
                                           JobStatus.CLEANING, JobStatus.VALIDATING):
        log.info("Duplicate request for %s — ignoring", subtitle_path)
        return jsonify({"status": "duplicate", "job_id": existing["id"]})

    job_id = str(uuid.uuid4())[:8]
    create_job(job_id, subtitle_path, media_path, language, source="bazarr")
    log.info("Queued job %s: %s", job_id, os.path.basename(subtitle_path))

    return jsonify({"status": "queued", "job_id": job_id})


@app.route("/bulk", methods=["POST"])
def bulk_scan():
    """Trigger a bulk scan of a directory for existing subtitles."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "no payload"}), 400

    directory = payload.get("directory", Config.MEDIA_PATH)
    sub_filter = payload.get("filter", "all")  # all, regular, forced, hi

    if not os.path.isdir(directory):
        return jsonify({"error": f"Directory not found: {directory}"}), 400

    count = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if not f.endswith(".srt"):
                continue

            subtitle_path = os.path.join(root, f)

            if sub_filter == "forced" and ".forced." not in f.lower():
                continue
            elif sub_filter == "regular" and (".forced." in f.lower() or
                                              ".hi." in f.lower() or
                                              ".sdh." in f.lower()):
                continue
            elif sub_filter == "hi" and ".hi." not in f.lower() and \
                    ".sdh." not in f.lower():
                continue

            media_path = _find_media_for_subtitle(subtitle_path)
            if not media_path:
                continue

            existing = get_job_by_subtitle(subtitle_path)
            if existing and existing["status"] == JobStatus.COMPLETED:
                continue

            job_id = str(uuid.uuid4())[:8]
            language = _extract_language(f)
            create_job(job_id, subtitle_path, media_path, language,
                       source="bulk")
            count += 1

    log.info("Bulk scan queued %d subtitles from %s (filter=%s)",
             count, directory, sub_filter)
    return jsonify({"status": "queued", "count": count})


@app.route("/jobs")
def list_jobs():
    jobs = get_recent_jobs()
    return jsonify(jobs)


@app.route("/stats")
def stats():
    return jsonify(get_stats())


def _find_media_for_subtitle(subtitle_path):
    """Find the media file that corresponds to a subtitle file."""
    directory = os.path.dirname(subtitle_path)
    sub_base = os.path.basename(subtitle_path)

    parts = sub_base.split(".")
    if len(parts) >= 3:
        media_stem = parts[0]
    else:
        media_stem = os.path.splitext(sub_base)[0]

    for ext in (".mkv", ".mp4", ".avi", ".m4v", ".ts"):
        candidate = os.path.join(directory, media_stem + ext)
        if os.path.isfile(candidate):
            return candidate

    for f in os.listdir(directory):
        if f.endswith((".mkv", ".mp4", ".avi", ".m4v", ".ts")):
            return os.path.join(directory, f)

    return None


def _extract_language(filename):
    """Extract language tag from subtitle filename."""
    parts = filename.lower().replace(".srt", "").split(".")
    for part in reversed(parts):
        if part in ("en", "eng", "english"):
            return "en"
        if part == "forced":
            continue
        if part in ("hi", "sdh", "cc"):
            continue
        if len(part) == 2 or len(part) == 3:
            return part
    return ""


def create_app():
    init_db()
    start_worker()
    return app
