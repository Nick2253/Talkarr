import contextlib
import json
import logging
import os
import uuid

from flask import Flask, jsonify, request

from .config import Config
from .db import (
    JobStatus,
    create_job,
    get_job,
    get_job_by_subtitle,
    get_recent_jobs,
    get_stats,
    init_db,
)
from .media import find_video_for_subtitle as _find_media_for_subtitle
from .paths import translate_path
from .worker import start_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
# Only signs the flash-message cookie; a fresh key per start just drops pending flashes.
app.secret_key = os.environ.get("TALKARR_SECRET_KEY") or os.urandom(32)


@app.before_request
def _check_webhook_secret():
    if request.path in ("/process", "/bulk") and Config.WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {Config.WEBHOOK_SECRET}":
            return jsonify({"error": "unauthorized"}), 401


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def _within_media(path):
    """True if ``path`` resolves inside MEDIA_PATH; Talkarr never touches files elsewhere."""
    root = os.path.realpath(Config.MEDIA_PATH)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


@app.route("/process", methods=["POST"])
def process():
    """Webhook endpoint for Bazarr post-processing."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "no payload"}), 400

    subtitle_path = translate_path(payload.get("subtitle", ""))
    media_path = translate_path(payload.get("media", ""))
    language = payload.get("language", "")

    if not subtitle_path:
        return jsonify({"error": "missing subtitle path"}), 400
    if not _within_media(subtitle_path):
        return jsonify({"error": f"subtitle is outside {Config.MEDIA_PATH}"}), 400

    existing = get_job_by_subtitle(subtitle_path)
    if existing and existing["status"] in (
        JobStatus.PENDING,
        JobStatus.CLASSIFYING,
        JobStatus.CLEANING,
        JobStatus.SYNCING,
        JobStatus.VERIFYING,
        JobStatus.VALIDATING,
    ):
        log.info("Duplicate request for %s — ignoring", subtitle_path)
        return jsonify({"status": "duplicate", "job_id": existing["id"]})

    job_id = str(uuid.uuid4())[:8]
    create_job(job_id, subtitle_path, media_path, language, source="bazarr")
    log.info("Queued job %s: %s", job_id, os.path.basename(subtitle_path))

    return jsonify({"status": "queued", "job_id": job_id})


class ScanError(Exception):
    """A scan request that can't be started; the message is shown to the user."""


def queue_scan(directory, sub_filter="all", requeue=False):
    """Queue every subtitle under ``directory`` (inside MEDIA_PATH) for checking."""
    if not _within_media(directory):
        raise ScanError(f"{directory} is outside the media library ({Config.MEDIA_PATH})")
    if not os.path.isdir(directory):
        raise ScanError(f"Folder not found: {directory}")

    count = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if not f.endswith(".srt"):
                continue
            lower = f.lower()
            is_forced = ".forced." in lower
            is_hi = ".hi." in lower or ".sdh." in lower
            if (
                (sub_filter == "forced" and not is_forced)
                or (sub_filter == "regular" and (is_forced or is_hi))
                or (sub_filter == "hi" and not is_hi)
            ):
                continue

            subtitle_path = os.path.join(root, f)
            media_path = _find_media_for_subtitle(subtitle_path)
            if not media_path:
                continue
            existing = get_job_by_subtitle(subtitle_path)
            if existing and existing["status"] == JobStatus.COMPLETED and not requeue:
                continue
            if existing and existing["status"] not in (JobStatus.COMPLETED, JobStatus.FAILED):
                continue  # already queued or being checked

            create_job(
                str(uuid.uuid4())[:8],
                subtitle_path,
                media_path,
                _extract_language(f),
                source="bulk",
            )
            count += 1

    log.info("Scan queued %d subtitles from %s (filter=%s)", count, directory, sub_filter)
    return count


@app.route("/bulk", methods=["POST"])
def bulk_scan():
    """Queue a scan of a directory for existing subtitles (API)."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "no payload"}), 400
    try:
        count = queue_scan(
            payload.get("directory", Config.MEDIA_PATH),
            payload.get("filter", "all"),
            requeue=bool(payload.get("requeue", False)),
        )
    except ScanError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "queued", "count": count})


@app.route("/jobs")
def list_jobs():
    review = {"1": True, "0": False}.get(request.args.get("review", ""))
    limit = min(int(request.args.get("limit", 100)), 1000)
    jobs = get_recent_jobs(limit=limit, verdict=request.args.get("verdict"), review=review)
    return jsonify([_decoded(j) for j in jobs])


@app.route("/jobs/<job_id>")
def job_detail(job_id):
    job = get_job(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(_decoded(job))


def _decoded(job):
    for key in ("report", "actions_taken", "validation_notes"):
        if job.get(key):
            with contextlib.suppress(ValueError):
                job[key] = json.loads(job[key])
    return job


@app.route("/stats")
def stats():
    return jsonify(get_stats())


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


from .ui import ui  # noqa: E402 - the blueprint imports queue_scan from this module

app.register_blueprint(ui)


def create_app():
    init_db()
    start_worker()
    return app
