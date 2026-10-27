"""Acting on proposals: apply (all, some, or adjusted), dismiss, revert, re-check.

Shared by the worker (automatic modes) and the web UI (by hand).
"""

import json
import logging
import os
import time

from . import db, fixes, settings
from .db import JobStatus

log = logging.getLogger(__name__)


class ReviewError(Exception):
    """A request that can't be carried out; the message is shown to the user."""


def _load(job_id):
    job = db.get_job(job_id)
    if job is None:
        raise ReviewError("no such job")
    proposal = json.loads(job["proposal"]) if job.get("proposal") else None
    if not proposal:
        raise ReviewError("this job has no proposed fixes")
    return job, proposal


def apply_job(job_id, indices=None, overrides=None, by="you"):
    """Apply fixes (all still-proposed ones if ``indices`` is None).

    ``by`` is "you" for a person, or the mode that applied them automatically.
    """
    job, proposal = _load(job_id)
    if indices is None:
        indices = [i for i, f in enumerate(proposal["fixes"]) if f["state"] == fixes.PROPOSED]
    if not indices:
        raise ReviewError("nothing left to apply")
    try:
        new_fixes, backup = fixes.apply(job, proposal, indices, overrides)
    except fixes.ApplyError as e:
        raise ReviewError(str(e)) from e
    backup["by"] = by
    proposal["fixes"] = new_fixes
    if os.path.isfile(job["subtitle_path"]):
        # The file is now the fixed one: the fixes still waiting apply to it, so applying
        # them later mustn't be refused as "changed since it was checked".
        proposal["fingerprint"] = fixes.fingerprint(job["subtitle_path"])
    history = json.loads(job["backup"]) if job.get("backup") else []
    history = history if isinstance(history, list) else [history]
    history.append(backup)
    db.update_job_applied(job_id, proposal, fixes.outcome(new_fixes), history)
    failed = [f for f in new_fixes if f["state"] == fixes.FAILED]
    later = [i for i in indices if new_fixes[i].get("retry_after") and new_fixes[i]["error"]]
    log.info("Job %s: applied by %s (%s)", job_id, by, fixes.outcome(new_fixes))
    if failed:
        raise ReviewError(f"{failed[0]['summary']} failed: {failed[0]['error']}")
    if later:
        fix = new_fixes[later[0]]
        raise ReviewError(f"{fix['summary']}: {fix['error']}; the automatic modes retry it")
    return new_fixes


def dismiss_job(job_id, indices=None):
    job, proposal = _load(job_id)
    for i, fix in enumerate(proposal["fixes"]):
        if (indices is None or i in indices) and fix["state"] == fixes.PROPOSED:
            fix["state"] = fixes.DISMISSED
    db.update_job_proposal(job_id, proposal, fixes.outcome(proposal["fixes"]))


def revert_job(job_id):
    """Restore the subtitle from the earliest backup and reopen the proposal."""
    job, proposal = _load(job_id)
    history = json.loads(job["backup"]) if job.get("backup") else []
    history = history if isinstance(history, list) else [history]
    if not history:
        raise ReviewError("nothing has been applied to this subtitle")
    try:
        fixes.revert(job, history[0])
    except fixes.ApplyError as e:
        raise ReviewError(str(e)) from e
    for fix in proposal["fixes"]:
        if fix["state"] in (fixes.APPLIED, fixes.FAILED):
            fix["state"], fix["error"] = fixes.PROPOSED, None
    proposal["fingerprint"] = fixes.fingerprint(job["subtitle_path"])
    db.update_job_applied(job_id, proposal, fixes.outcome(proposal["fixes"]), [])
    log.info("Job %s: reverted to the original", job_id)


def recheck_job(job_id):
    """Queue the subtitle to be analysed again (e.g. after it changed on disk)."""
    job = db.get_job(job_id)
    if job is None:
        raise ReviewError("no such job")
    db.set_job_source(job_id, "manual")  # ahead of any scan backlog
    db.update_job_status(job_id, JobStatus.PENDING)


# --- retrying ---------------------------------------------------------------------------
# A fix left waiting because Bazarr didn't answer is tried again once its time comes.

RETRY_SCAN_SECONDS = 300
_last_retry_scan = [0.0]


def retry_step(clock=time.time):
    """Apply one subtitle whose automatic fix is due a retry; at most every few minutes."""
    mode = settings.get_mode()
    now = clock()
    if mode == settings.Mode.MANUAL or now - _last_retry_scan[0] < RETRY_SCAN_SECONDS:
        return False
    _last_retry_scan[0] = now
    for job, indices in waiting_for(mode, now):
        due = json.loads(job["proposal"])["fixes"]
        if any("retry_after" in due[i] for i in indices):
            try:
                apply_job(job["id"], indices, by=mode)
            except ReviewError as e:
                log.warning("Retry: job %s not applied: %s", job["id"], e)
            return True
    return False


# --- catching up -----------------------------------------------------------------------
# A mode only acts on subtitles checked after it is chosen. Catching up applies what it
# would have applied to the fixes already waiting, one subtitle at a time.

CATCH_UP = "catch_up"  # settings key: set while a catch-up runs
# Each removal of a Bazarr download makes Bazarr search its providers again.
REMOVAL_PACE_SECONDS = 10.0


def waiting_for(mode, now=None):
    """``[(job, indices)]``: checked subtitles with fixes ``mode`` applies on its own
    (not counting fixes waiting for their retry time)."""
    now = time.time() if now is None else now
    found = []
    for job in db.get_jobs(outcome=["pending", "partial"], limit=1_000_000):
        if job["status"] != JobStatus.COMPLETED or not job.get("proposal"):
            continue
        proposed = json.loads(job["proposal"])["fixes"]
        indices = [
            i
            for i in fixes.auto_indices(mode, proposed)
            if proposed[i].get("retry_after", 0) <= now
        ]
        if indices:
            found.append((job, indices))
    return sorted(found, key=lambda item: (item[0]["created_at"], item[0]["id"]))  # oldest first


def start_catch_up():
    mode = settings.get_mode()
    if mode == settings.Mode.MANUAL:
        raise ReviewError("Manual applies nothing on its own; pick Safe Auto or Full Auto")
    db.set_setting(CATCH_UP, "1")
    return len(waiting_for(mode))


def stop_catch_up():
    db.set_setting(CATCH_UP, "")


def catch_up_running():
    return bool(db.get_setting(CATCH_UP))


def catch_up_step(sleep=time.sleep):
    """Apply the next waiting subtitle's fixes. Returns False once there are none left
    (or the mode was set back to Manual), which ends the catch-up."""
    mode = settings.get_mode()
    waiting = waiting_for(mode) if mode != settings.Mode.MANUAL else []
    if not waiting:
        stop_catch_up()
        return False
    job, indices = waiting[0]
    kinds = {json.loads(job["proposal"])["fixes"][i]["kind"] for i in indices}
    try:
        apply_job(job["id"], indices, by=mode)
    except ReviewError as e:
        log.warning("Catch-up: job %s not applied: %s", job["id"], e)
        if any(j["id"] == job["id"] for j, _ in waiting_for(mode)):
            recheck_job(job["id"])  # e.g. the file changed: check it again, don't retry
    if "remove" in kinds:
        sleep(REMOVAL_PACE_SECONDS)
    return True
