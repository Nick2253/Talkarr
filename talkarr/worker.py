import logging
import threading
import time

from .config import Config
from .db import (get_jobs_by_status, update_job_status, update_job_classification,
                 update_job_sync, update_job_validation, update_job_actions,
                 JobStatus, SubType)
from .classifier import classify
from .sync import sync_subtitle
from .cleaner import clean_subtitle
from .validator import validate
from .notify import send_discord_notification

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

    if Config.CLEAN_ENABLED:
        update_job_status(job["id"], JobStatus.CLEANING)
        clean_result = clean_subtitle(job["subtitle_path"])
        if clean_result.success and clean_result.changed:
            actions.append(
                f"cleaned: {clean_result.ads_removed} ads removed, "
                f"encoding_fixed={clean_result.encoding_fixed}")
        elif not clean_result.success:
            log.warning("Clean failed for %s: %s",
                        job["subtitle_path"], clean_result.error)

    sync_confidence = None
    if Config.SYNC_ENABLED:
        update_job_status(job["id"], JobStatus.SYNCING)
        sync_result = sync_subtitle(
            job["subtitle_path"], job["media_path"], sub_type)

        if sync_result.skipped:
            actions.append("sync: skipped (forced sub)")
        elif sync_result.success:
            update_job_sync(job["id"], sync_result.offset,
                            sync_result.confidence)
            sync_confidence = sync_result.confidence
            offset_str = f"{sync_result.offset:.2f}s" if sync_result.offset else "0s"
            actions.append(f"synced: offset={offset_str}")
        else:
            log.warning("Sync failed for %s: %s",
                        job["subtitle_path"], sync_result.error)
            actions.append(f"sync failed: {sync_result.error}")

    update_job_status(job["id"], JobStatus.VALIDATING)
    val_result = validate(job["subtitle_path"], sub_type, sync_confidence)
    update_job_validation(job["id"], val_result.score,
                          {"issues": val_result.issues,
                           "warnings": val_result.warnings})

    if val_result.passed:
        update_job_actions(job["id"], actions)
        update_job_status(job["id"], JobStatus.COMPLETED)
        log.info("Job %s completed: score=%.2f, actions=%s",
                 job["id"], val_result.score, actions)
    else:
        actions.append(f"validation failed: score={val_result.score:.2f}, "
                       f"issues={val_result.issues}")
        update_job_actions(job["id"], actions)

        if _should_blacklist(val_result):
            update_job_status(job["id"], JobStatus.BLACKLISTED)
            log.warning("Job %s blacklisted: %s",
                        job["id"], val_result.issues)
            send_discord_notification(
                f"Talkarr — Subtitle failed validation\n"
                f"File: {job['subtitle_path'].split('/')[-1]}\n"
                f"Score: {val_result.score:.2f}\n"
                f"Issues: {', '.join(val_result.issues)}")
        else:
            update_job_status(job["id"], JobStatus.COMPLETED)
            log.info("Job %s completed with warnings: %s",
                     job["id"], val_result.warnings)


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
