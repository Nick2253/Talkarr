"""Background job loop: analyse each queued subtitle, propose fixes, apply per mode.

Analysis never writes to the library. Cleaning and syncing run as trials (ffsubsync
into a scratch file), forced subtitles are verified, and the results become a
proposal (talkarr/fixes.py). The current mode then decides which fixes are applied
straight away; the rest wait on the review page.
"""

import logging
import os
import tempfile
import threading
import time

from . import fixes, mods, review, settings
from .classifier import classify
from .cleaner import clean_subtitle
from .config import Config
from .db import (
    JobStatus,
    SubType,
    next_pending,
    update_job_actions,
    update_job_classification,
    update_job_proposal,
    update_job_report,
    update_job_status,
    update_job_sync,
    update_job_validation,
)
from .forced.service import verify_forced
from .media import find_video_for_subtitle
from .sync import confirm_sync, run_ffsubsync
from .validator import validate

log = logging.getLogger(__name__)

# Applying a waiting fix takes about a second; checking a subtitle can take minutes.
CATCH_UP_SECONDS = 30

GONE = "the subtitle was replaced or deleted before it was checked"


def process_pending_jobs():
    """Check queued subtitles one at a time, re-reading the queue after each, so a
    re-check or a new subtitle from Bazarr doesn't wait behind a whole scan. If a
    catch-up is running, spend up to ``CATCH_UP_SECONDS`` applying waiting fixes after
    each, so a steady stream of downloads doesn't hold it up."""
    while (job := next_pending()) is not None:
        try:
            _process_job(job)
        except Exception as e:
            log.exception("Job %s failed: %s", job["id"], e)
            update_job_status(job["id"], JobStatus.FAILED, str(e))
        _catch_up(CATCH_UP_SECONDS)
    _catch_up(CATCH_UP_SECONDS)


def _catch_up(seconds, clock=time.monotonic):
    end = clock() + seconds
    while review.catch_up_running() and review.catch_up_step() and clock() < end:
        pass


def _process_job(job):
    path = job["subtitle_path"]
    notes = []
    if not os.path.isfile(path):
        # Bazarr replaced or deleted it before its turn came; its replacement is queued
        # separately.
        update_job_status(job["id"], JobStatus.SKIPPED, GONE)
        return

    update_job_status(job["id"], JobStatus.CLASSIFYING)
    sub_type = classify(path, job.get("language"))
    update_job_classification(job["id"], sub_type)

    clean_result = None
    if Config.CLEAN_ENABLED:
        update_job_status(job["id"], JobStatus.CLEANING)
        clean_result = clean_subtitle(path, write=False)
        if not clean_result.success:
            notes.append(f"cleaning check failed: {clean_result.error}")

    mods_result = None
    chosen_mods = mods.parse(Config.SUBTITLE_MODS)[0]
    if chosen_mods:
        mods_result = mods.run(path, chosen_mods, sub_type, job.get("language") or "")
        if not mods_result.success:
            notes.append(f"modifications check failed: {mods_result.error}")

    sync_result = None
    if Config.SYNC_ENABLED and sub_type != SubType.FORCED:
        update_job_status(job["id"], JobStatus.SYNCING)
        sync_result = _trial_sync(path, job.get("media_path"))
        if sync_result.success:
            update_job_sync(job["id"], sync_result.offset, sync_result.confidence)
        else:
            notes.append(f"sync check failed: {sync_result.error}")

    decision = None
    if sub_type == SubType.FORCED and Config.FORCED_VERIFY_ENABLED:
        update_job_status(job["id"], JobStatus.VERIFYING)
        decision = _verify_forced(job)

    update_job_status(job["id"], JobStatus.VALIDATING)
    val = validate(path, sub_type, sync_result.confidence if sync_result else None)
    update_job_validation(job["id"], val.score, {"issues": val.issues, "warnings": val.warnings})

    proposed = fixes.build(
        path, sub_type, clean=clean_result, sync=sync_result, decision=decision, mods=mods_result
    )
    proposal = {"fingerprint": fixes.fingerprint(path), "fixes": proposed}
    update_job_proposal(job["id"], proposal, fixes.outcome(proposed))
    update_job_actions(job["id"], notes)
    update_job_status(job["id"], JobStatus.COMPLETED)

    mode = settings.get_mode()
    auto = fixes.auto_indices(mode, proposed)
    if auto:
        log.info("Job %s: %s applies %d of %d fixes", job["id"], mode, len(auto), len(proposed))
        review.apply_job(job["id"], auto, by=mode)
    log.info("Job %s: %d fixes proposed", job["id"], len(proposed))


def _verify_forced(job):
    """Verify a forced subtitle against its film and record the verdict."""
    video = job.get("media_path")
    if not video or not os.path.isfile(video):
        video = find_video_for_subtitle(job["subtitle_path"])
    if not video:
        update_job_report(job["id"], "unverifiable", False, {"reason": "no video found"})
        return None
    decision, details = verify_forced(
        job["subtitle_path"], video, Config.CACHE_DIR, ml_url=Config.ML_URL
    )
    update_job_report(
        job["id"], str(decision.verdict), decision.review, {**decision.as_dict(), **details}
    )
    log.info("Job %s forced verdict: %s (%s)", job["id"], decision.verdict, decision.reason)
    return decision


def _trial_sync(subtitle_path, media_path):
    """Run ffsubsync into a scratch file to learn the correction, then discard the file."""
    if not media_path or not os.path.isfile(media_path):
        media_path = find_video_for_subtitle(subtitle_path)
    if not media_path:
        from .sync import SyncResult

        return SyncResult(success=False, error="no video found next to the subtitle")
    scratch = os.path.join(Config.CACHE_DIR, "tmp")
    os.makedirs(scratch, exist_ok=True)
    fd, out = tempfile.mkstemp(suffix=".srt", dir=scratch)
    os.close(fd)
    # ffsubsync saves the film's speech next to its reference: link the video into the
    # scratch folder so that lands there, not in the library, and the checks reuse it.
    stem = out[:-4] + ".ref"
    link = stem + os.path.splitext(media_path)[1]
    speech = stem + ".npz"
    try:
        os.symlink(os.path.abspath(media_path), link)
        result = run_ffsubsync(subtitle_path, link, out, serialize_speech=True)
        if result.success:
            result.confirmed = confirm_sync(
                subtitle_path,
                speech if os.path.isfile(speech) else link,
                result.framerate or 1.0,
                result.offset or 0.0,
                scratch,
            )
        return result
    finally:
        for path in (out, link, speech):
            if os.path.lexists(path):
                os.unlink(path)


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
