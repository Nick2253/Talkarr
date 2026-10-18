"""Acting on proposals: apply (all, some, or adjusted), dismiss, revert, re-check.

Shared by the worker (automatic modes) and the web UI (by hand).
"""

import json
import logging

from . import db, fixes
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
    history = json.loads(job["backup"]) if job.get("backup") else []
    history = history if isinstance(history, list) else [history]
    history.append(backup)
    db.update_job_applied(job_id, proposal, fixes.outcome(new_fixes), history)
    failed = [f for f in new_fixes if f["state"] == fixes.FAILED]
    log.info("Job %s: applied by %s (%s)", job_id, by, fixes.outcome(new_fixes))
    if failed:
        raise ReviewError(f"{failed[0]['summary']} failed: {failed[0]['error']}")
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
    db.update_job_status(job_id, JobStatus.PENDING)
