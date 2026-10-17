import logging
import os
import tempfile
import threading
import time

from .classifier import classify
from .cleaner import clean_subtitle
from .config import Config
from .db import (
    JobStatus,
    SubType,
    get_jobs_by_status,
    update_job_actions,
    update_job_classification,
    update_job_report,
    update_job_status,
    update_job_sync,
    update_job_validation,
)
from .forced.service import verify_forced
from .media import find_video_for_subtitle
from .notify import send_discord_notification
from .sync import run_ffsubsync, sync_subtitle
from .validator import validate

log = logging.getLogger(__name__)


def process_pending_jobs():
    jobs = get_jobs_by_status(JobStatus.PENDING)
    for job in jobs:
        try:
            _process_job(job)
        except Exception as e:
            log.exception("Job %s failed: %s", job["id"], e)
            update_job_status(job["id"], JobStatus.FAILED, str(e))


def _process_job(job):
    actions = []

    update_job_status(job["id"], JobStatus.CLASSIFYING)
    sub_type = classify(job["subtitle_path"], job.get("language"))
    update_job_classification(job["id"], sub_type)
    actions.append(f"classified: {sub_type}")

    dry = Config.DRY_RUN
    if Config.CLEAN_ENABLED:
        update_job_status(job["id"], JobStatus.CLEANING)
        clean_result = clean_subtitle(job["subtitle_path"], write=not dry)
        if clean_result.success and clean_result.changed:
            actions.append(
                f"{'would clean' if dry else 'cleaned'}: {clean_result.ads_removed} ads removed, "
                f"encoding_fixed={clean_result.encoding_fixed}, "
                f"source_encoding={clean_result.source_encoding}"
            )
        elif not clean_result.success:
            log.warning("Clean failed for %s: %s", job["subtitle_path"], clean_result.error)

    sync_confidence = None
    if Config.SYNC_ENABLED:
        update_job_status(job["id"], JobStatus.SYNCING)
        if dry and sub_type != SubType.FORCED:
            sync_result = _trial_sync(job["subtitle_path"], job["media_path"])
        else:
            sync_result = sync_subtitle(job["subtitle_path"], job["media_path"], sub_type)

        if sync_result.skipped:
            actions.append("sync: skipped (forced sub)")
        elif sync_result.success:
            update_job_sync(job["id"], sync_result.offset, sync_result.confidence)
            sync_confidence = sync_result.confidence
            offset_str = f"{sync_result.offset:.2f}s" if sync_result.offset else "0s"
            actions.append(f"{'would sync' if dry else 'synced'}: offset={offset_str}")
        else:
            log.warning("Sync failed for %s: %s", job["subtitle_path"], sync_result.error)
            actions.append(f"sync failed: {sync_result.error}")

    if sub_type == SubType.FORCED and Config.FORCED_VERIFY_ENABLED:
        update_job_status(job["id"], JobStatus.VERIFYING)
        actions.append(_verify_forced(job))

    update_job_status(job["id"], JobStatus.VALIDATING)
    val_result = validate(job["subtitle_path"], sub_type, sync_confidence)
    update_job_validation(
        job["id"], val_result.score, {"issues": val_result.issues, "warnings": val_result.warnings}
    )

    if val_result.passed:
        update_job_actions(job["id"], actions)
        update_job_status(job["id"], JobStatus.COMPLETED)
        log.info("Job %s completed: score=%.2f, actions=%s", job["id"], val_result.score, actions)
    else:
        actions.append(
            f"validation failed: score={val_result.score:.2f}, issues={val_result.issues}"
        )
        update_job_actions(job["id"], actions)

        if _should_blacklist(val_result):
            update_job_status(job["id"], JobStatus.FLAGGED if dry else JobStatus.BLACKLISTED)
            log.warning("Job %s blacklisted: %s", job["id"], val_result.issues)
            send_discord_notification(
                f"Talkarr — Subtitle failed validation\n"
                f"File: {job['subtitle_path'].split('/')[-1]}\n"
                f"Score: {val_result.score:.2f}\n"
                f"Issues: {', '.join(val_result.issues)}"
            )
        else:
            update_job_status(job["id"], JobStatus.COMPLETED)
            log.info("Job %s completed with warnings: %s", job["id"], val_result.warnings)


def _verify_forced(job):
    """Verify a forced subtitle against its film; record the verdict. Never writes."""
    video = job.get("media_path")
    if not video or not os.path.isfile(video):
        video = find_video_for_subtitle(job["subtitle_path"])
    if not video:
        update_job_report(job["id"], "unverifiable", False, {"reason": "no video found"})
        return "forced: unverifiable (no video found)"
    decision, details = verify_forced(
        job["subtitle_path"], video, Config.CACHE_DIR, ml_url=Config.ML_URL
    )
    update_job_report(
        job["id"], str(decision.verdict), decision.review, {**decision.as_dict(), **details}
    )
    log.info("Job %s forced verdict: %s (%s)", job["id"], decision.verdict, decision.reason)
    flag = " [review]" if decision.review else ""
    # Applying a forced-subtitle correction is not implemented yet: report only.
    return f"forced: {decision.verdict}{flag}"


def _trial_sync(subtitle_path, media_path):
    """Dry-run sync: run ffsubsync into a scratch file to learn the offset, then discard it."""
    if not media_path or not os.path.isfile(media_path):
        return sync_subtitle(subtitle_path, media_path, SubType.REGULAR)  # reports the error
    scratch = os.path.join(Config.CACHE_DIR, "tmp")
    os.makedirs(scratch, exist_ok=True)
    fd, out = tempfile.mkstemp(suffix=".srt", dir=scratch)
    os.close(fd)
    try:
        return run_ffsubsync(subtitle_path, media_path, out)
    finally:
        if os.path.exists(out):
            os.unlink(out)


def _should_blacklist(val_result):
    for issue in val_result.issues:
        if "mislabeled" in issue.lower():
            return True
        if "empty" in issue.lower():
            return True
    return val_result.score < 0.2


def start_worker():
    def _loop():
        while True:
            try:
                process_pending_jobs()
            except Exception:
                log.exception("Worker loop error")
            time.sleep(5)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    log.info("Worker started")
