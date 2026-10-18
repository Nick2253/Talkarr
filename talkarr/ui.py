"""Web UI: review queue, job detail, library, activity, settings.

Server-rendered (Jinja); talkarr.js only enhances. Every change goes through
talkarr/review.py, the same code the automatic modes use.
"""

import json
import os
import time
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from . import db, fixes, review, settings
from .config import Config
from .forced import subtitles as S
from .settings import Mode

ui = Blueprint("ui", __name__)

ASSET_VERSION = str(int(time.time()))

VERDICT_LABELS = [
    ("mislabelled", "Full subtitle saved as forced"),
    ("mismatch", "Wrong film or cut"),
    ("retimed", "Needs retiming"),
    ("in_sync", "In sync"),
    ("unverifiable", "Can't verify"),
]
VERDICT_NAMES = {
    "mislabelled": "Not a forced track",
    "mismatch": "Wrong film or cut",
    "retimed": "Needs retiming",
    "in_sync": "In sync",
    "unverifiable": "Can't verify",
}
OUTCOME_NAMES = {
    "pending": "Waiting for you",
    "partial": "Partly applied",
    "applied": "Applied",
    "dismissed": "Dismissed",
    "failed": "Failed",
    "nothing": "Nothing to fix",
}
TIER_NAMES = {
    fixes.SAFE: "Safe: Safe Auto and Full Auto apply this",
    fixes.CONFIDENT: "Confident: Full Auto applies this",
    fixes.UNCERTAIN: "Uncertain: waits for you in every mode",
}
TIER_HELP = {
    fixes.SAFE: "Corroborated and reversible. Safe Auto and Full Auto apply it on their own.",
    fixes.CONFIDENT: "One strong check, or it removes or renames a file. Full Auto applies it.",
    fixes.UNCERTAIN: "The evidence isn't conclusive. No mode applies it without you.",
}
MODE_TEXT = {
    Mode.MANUAL: "Checks everything and proposes fixes. Nothing changes until you apply it.",
    Mode.SAFE_AUTO: "Applies corroborated, reversible fixes on its own: cleaning, and forced "
    "retimes both checks agree on. Everything else waits for you.",
    Mode.FULL_AUTO: "Applies every fix it is confident about, including syncs and removing or "
    "renaming mislabelled files as configured. Only uncertain cases wait for you.",
}
MODE_SENTENCE = {
    Mode.MANUAL: "Manual: nothing changes until you apply it.",
    Mode.SAFE_AUTO: "Safe Auto: corroborated, reversible fixes are applied on their own.",
    Mode.FULL_AUTO: "Full Auto: confident fixes are applied on their own.",
}
FRAMERATE_OPTIONS = [
    (1.0, "No change"),
    (23.976 / 25, "25 → 23.976 (PAL to film)"),
    (24 / 25, "25 → 24"),
    (23.976 / 24, "24 → 23.976"),
    (25 / 23.976, "23.976 → 25"),
    (25 / 24, "24 → 25"),
    (24 / 23.976, "23.976 → 24"),
]
WAITING = ["pending", "partial", "failed"]
BUSY_STATUSES = ("pending", "classifying", "cleaning", "syncing", "verifying", "validating")


# --- request plumbing -------------------------------------------------------------------


@ui.before_request
def _same_origin_posts():
    """Reject form posts that come from another site (CSRF)."""
    if request.method != "POST":
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if source and urlparse(source).netloc != request.host:
        abort(403)
    return None


@ui.app_context_processor
def _globals():
    mode = settings.get_mode()
    return {
        "mode": mode,
        "mode_label": Mode.LABELS[mode],
        "mode_sentence": MODE_SENTENCE[mode],
        "waiting_count": sum(db.count_by("outcome").get(k, 0) for k in WAITING),
        "asset_version": ASSET_VERSION,
        "verdict_names": VERDICT_NAMES,
        "verdict_labels": VERDICT_LABELS,
        "outcome_names": OUTCOME_NAMES,
        "tier_names": TIER_NAMES,
        "tier_help": TIER_HELP,
    }


@ui.app_template_filter("ago")
def _ago(ts):
    if not ts:
        return "–"
    seconds = max(0, time.time() - float(ts))
    for limit, unit in ((60, "s"), (3600, "min"), (86400, "h")):
        if seconds < limit:
            divisor = {"s": 1, "min": 60, "h": 3600}[unit]
            return "just now" if unit == "s" else f"{int(seconds // divisor)} {unit} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


@ui.app_template_filter("tc")
def _tc(seconds):
    seconds = max(0.0, float(seconds or 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", ",")


@ui.app_template_filter("tc_short")
def _tc_short(seconds):
    seconds = int(float(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _back(default):
    target = request.form.get("next") or ""
    return redirect(target if target.startswith("/") and not target.startswith("//") else default)


# --- job presentation --------------------------------------------------------------------


def _loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def title_of(path):
    """Film folder name; for TV, "Show · Season 1"."""
    parent = os.path.dirname(path)
    name = os.path.basename(parent)
    if name.lower().startswith(("season", "specials")):
        return f"{os.path.basename(os.path.dirname(parent))} · {name}"
    return name or os.path.basename(path)


# What a subtitle's state is *now*: the verdict says what Talkarr found, the outcome
# says what has happened since. Shown everywhere instead of the bare verdict.
# key -> (label, CSS class, library filter)
STATES = {
    "fixed": ("Fixed", "fixed", {"outcome": "applied"}),
    "in_sync": ("In sync", "in_sync", {"verdict": "in_sync"}),
    "needs_retiming": ("Needs retiming", "retimed", {"verdict": "retimed", "outcome": "pending"}),
    "not_forced": (
        "Not a forced track",
        "mislabelled",
        {"verdict": "mislabelled", "outcome": "pending"},
    ),
    "waiting": ("Fix waiting", "retimed", {"outcome": "pending"}),
    "failed": ("Fix failed", "mismatch", {"outcome": "failed"}),
    "mismatch": ("Wrong film or cut", "mismatch", {"verdict": "mismatch"}),
    "unverifiable": ("Can't verify", "unverifiable", {"verdict": "unverifiable"}),
    "left": ("Left as is", "left", {"outcome": "dismissed"}),
    "fine": ("Nothing to fix", "in_sync", {"outcome": "nothing"}),
}
STATE_ORDER = [
    "fixed",
    "in_sync",
    "fine",
    "needs_retiming",
    "not_forced",
    "waiting",
    "failed",
    "mismatch",
    "unverifiable",
    "left",
]


def state_of(job):
    outcome, verdict = job.get("outcome"), job.get("verdict")
    if outcome == "applied":
        return "fixed"
    if outcome == "failed":
        return "failed"
    if outcome == "dismissed":
        return "left"
    if outcome in ("pending", "partial"):
        return {"retimed": "needs_retiming", "mislabelled": "not_forced"}.get(verdict, "waiting")
    if verdict in ("in_sync", "mismatch", "unverifiable"):
        return verdict
    if verdict == "mislabelled":
        return "not_forced"  # flagged only (MISLABELLED_ACTION=flag)
    return "fine" if outcome == "nothing" else None


def present(job):
    job = dict(job)
    proposal = _loads(job.get("proposal"), {})
    report = _loads(job.get("report"), {})
    history = _loads(job.get("backup"), [])
    history = history if isinstance(history, list) else [history]
    job["fixes"] = proposal.get("fixes", []) if isinstance(proposal, dict) else []
    job["proposed_count"] = sum(1 for f in job["fixes"] if f["state"] == fixes.PROPOSED)
    job["title"] = title_of(job["subtitle_path"])
    job["file"] = os.path.basename(job["subtitle_path"])
    job["report_data"] = report
    job["history"] = history
    job["applied_by"] = Mode.LABELS.get(history[-1].get("by"), "you") if history else None
    job["can_revert"] = bool(history)
    job["state"] = state_of(job)
    job["state_label"], job["state_class"], _ = STATES.get(job["state"], ("", "", {}))
    job["reason"] = plain_reason(job, report)
    job["detail_reason"] = report.get("reason") or _regular_reason(job)
    job["relpath"] = _relative(job["subtitle_path"])
    return job


def _relative(path):
    root = os.path.realpath(Config.MEDIA_PATH)
    real = os.path.realpath(path)
    return os.path.relpath(real, root) if real.startswith(root + os.sep) else path


AUDIO_PHRASES = (
    ("audio broadly agrees", "the audio broadly agrees"),
    ("audio agrees", "the audio agrees"),
    ("audio independently finds the same", "the audio finds the same fix"),
    ("but audio disagrees", "but the audio disagrees"),
    ("audio couldn't confirm", "the audio couldn't confirm it"),
)


def plain_reason(job, report):
    """One plain sentence for the queue; the technical reason stays on the detail page."""
    verdict = job.get("verdict")
    if verdict in ("retimed", "in_sync"):
        text, audio = report.get("text") or {}, report.get("audio") or {}
        parts = []
        if text.get("verdict") in ("retimed", "in_sync") and text.get("forced_cues"):
            parts.append(
                f"{text.get('inliers')} of {text.get('forced_cues')} lines match the full subtitle"
            )
        elif audio.get("verdict") in ("retimed", "in_sync") and audio.get("forced_cues"):
            parts.append(
                f"{audio.get('inliers')} of {audio.get('forced_cues')} lines sit on foreign "
                "speech in the audio"
            )
        reason = report.get("reason", "")
        for needle, phrase in AUDIO_PHRASES:
            if needle in reason and parts:
                parts.append(phrase)
                break
        if parts:
            return "; ".join(parts) + "."
        return report.get("reason", "")
    if verdict == "mislabelled":
        cues = report.get("forced_cues") or (report.get("text") or {}).get("forced_cues")
        what = (
            "a section of a full subtitle"
            if "section" in report.get("reason", "")
            else "a full subtitle"
        )
        return f"It is {what} saved as forced" + (f" ({cues} lines)." if cues else ".")
    if verdict == "mismatch":
        return (
            "Its lines are in the full subtitle, but their timing doesn't fit this film. "
            "It may be from another cut."
        )
    if verdict == "unverifiable":
        if not report.get("reference") and "text" not in report:
            return "No full subtitle to compare with, and the audio check couldn't tell."
        return "Neither the full subtitle nor the audio could confirm it."
    return _regular_reason(job)


def _regular_reason(job):
    if job.get("sync_confidence") is not None and job.get("sync_offset") is not None:
        return (
            f"ffsubsync would move it {job['sync_offset']:+.2f} s "
            f"(confidence {job['sync_confidence']:.2f})"
        )
    return ""


# --- pages --------------------------------------------------------------------------------


@ui.route("/")
def review_page():
    waiting = [present(j) for j in db.get_jobs(outcome=WAITING)]
    flagged_rows = db.get_jobs(outcome="nothing", verdict=["mismatch", "mislabelled"])
    flagged_rows += [
        j
        for j in db.get_jobs(outcome="nothing", review=True)
        if j["id"] not in {f["id"] for f in flagged_rows}
    ]
    forced_states = {}
    for row in db.get_jobs(sub_type="forced", limit=100000):
        key = state_of(row)
        if key:
            forced_states[key] = forced_states.get(key, 0) + 1
    states = [
        (k, STATES[k][0], STATES[k][1], forced_states[k], STATES[k][2])
        for k in STATE_ORDER
        if forced_states.get(k)
    ]
    statuses = db.count_by("status")
    return render_template(
        "review.html",
        nav="review",
        waiting=waiting,
        fix_count=sum(j["proposed_count"] for j in waiting),
        flagged=[present(j) for j in flagged_rows],
        states=states,
        forced_total=sum(n for *_, n, _ in states),
        checked=statuses.get("completed", 0),
        busy=sum(statuses.get(s, 0) for s in BUSY_STATUSES),
    )


@ui.route("/library")
def library_page():
    args = request.args
    q, kind = args.get("q") or None, args.get("kind") or None
    verdict, outcome = args.get("verdict") or None, args.get("outcome") or None
    limit = 300
    jobs = [
        present(j)
        for j in db.get_jobs(outcome=outcome, verdict=verdict, sub_type=kind, search=q, limit=limit)
    ]
    statuses = db.count_by("status")
    busy = sum(statuses.get(s, 0) for s in BUSY_STATUSES)
    done = statuses.get("completed", 0) + statuses.get("failed", 0)
    return render_template(
        "library.html",
        nav="library",
        jobs=jobs,
        limit=limit,
        q=q,
        kind=kind,
        verdict=verdict,
        outcome=outcome,
        busy=busy,
        done=done,
        scan_dirs=_scan_dirs(),
    )


def _scan_dirs():
    """(path, label) pairs: each top-level library folder, then the whole library."""
    root = Config.MEDIA_PATH
    try:
        children = sorted(
            d
            for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
        )
    except OSError:
        children = []
    return [(os.path.join(root, d), d) for d in children] + [(root, "Whole library")]


@ui.route("/activity")
def activity_page():
    outcome = request.args.get("outcome") or None
    wanted = [outcome] if outcome else ["applied", "partial", "failed", "dismissed"]
    jobs = [present(j) for j in db.get_jobs(outcome=wanted)]
    return render_template("activity.html", nav="activity", jobs=jobs, outcome=outcome)


@ui.route("/job/<job_id>")
def job_page(job_id):
    row = db.get_job(job_id)
    if row is None:
        abort(404)
    job = present(row)
    report = job["report_data"]
    ribbon = report.get("ribbon")
    cues, cue_total = _cue_preview(job)
    adjust = _adjust(job)
    scale, offset = (adjust["scale"], adjust["offset"]) if adjust else (1.0, 0.0)
    cue_data = {
        "runtime": (ribbon or {}).get("runtime", 0),
        "cues": (ribbon or {}).get("cues", []),
        "speech": (ribbon or {}).get("speech", []),
        "scale": scale,
        "offset": offset,
    }
    raw = {k: v for k, v in report.items() if k != "ribbon"}
    return render_template(
        "job.html",
        nav="",
        job=job,
        title=job["title"],
        reason=job["reason"],
        fixes=job["fixes"],
        proposed_count=job["proposed_count"],
        ribbon=ribbon,
        cues=cues,
        cue_total=cue_total,
        adjust=adjust,
        framerates=FRAMERATE_OPTIONS,
        cue_data=cue_data,
        evidence=_evidence(job),
        history=_history(job),
        raw=json.dumps(raw, indent=2) if raw else "",
        no_fix_text=_no_fix_text(job),
    )


def _cue_preview(job, limit=8):
    path = job["subtitle_path"]
    if not os.path.isfile(path) and job["history"]:
        path = job["history"][0].get("copy", "")
    try:
        cues = S.load(path)
    except (OSError, ValueError):
        return [], 0
    preview = [
        {"n": i + 1, "start": c.start, "text": c.text.replace("\n", " ")[:120]}
        for i, c in enumerate(cues[:limit])
    ]
    return preview, len(cues)


def _adjust(job):
    for i, f in enumerate(job["fixes"]):
        if f["state"] == fixes.PROPOSED and f["kind"] in ("retime", "sync"):
            scale = float(f["params"].get("scale", 1.0)) if f["kind"] == "retime" else 1.0
            offset = float(f["params"].get("offset", 0.0))
            return {
                "index": i,
                "scale": scale,
                "offset": offset,
                "proposed_label": fixes.retime_summary(scale, offset).lower(),
            }
    return None


def _evidence(job):
    r = job["report_data"]
    items = []
    if job["sub_type"] == "forced":
        sync = r.get("reference_sync")
        if not r.get("reference") and "text" not in r:
            items.append(("neutral", "No full English subtitle beside it to compare with."))
        elif sync:
            if sync.get("ok"):
                rate = sync.get("framerate") or 1.0
                extra = f", rescaled ×{rate:.3f}" if abs(rate - 1.0) > 1e-3 else ""
                items.append(
                    (
                        "waveform",
                        f"Full English subtitle synced to the audio "
                        f"(moved {sync.get('offset') or 0:+.2f} s{extra}).",
                    )
                )
            else:
                items.append(
                    (
                        "tally",
                        f"The full subtitle couldn't be synced to the audio: {sync.get('error')}",
                    )
                )
        if r.get("text"):
            t = r["text"]
            items.append((_color(t.get("verdict")), f"Text check: {t.get('reason')}"))
        analysis = r.get("audio_analysis")
        if analysis and not analysis.get("ok"):
            items.append(("tally", f"Audio analysis failed: {analysis.get('error')}"))
        if r.get("audio"):
            a = r["audio"]
            items.append((_color(a.get("verdict")), f"Audio check: {a.get('reason')}"))
        elif not Config.ML_URL:
            items.append(("neutral", "Audio check is off (TALKARR_ML_URL is not set)."))
    else:
        if job.get("sync_offset") is not None:
            items.append(
                (
                    "subtitle" if abs(job["sync_offset"]) >= Config.SYNC_MIN_OFFSET else "waveform",
                    f"Trial sync: {_regular_reason(job)}.",
                )
            )
    validation = _loads(job.get("validation_notes"), {})
    if isinstance(validation, dict):
        for issue in validation.get("issues", []):
            items.append(("tally", f"Validation: {issue}"))
        for warning in validation.get("warnings", []):
            items.append(("subtitle", f"Validation: {warning}"))
    for note in _loads(job.get("actions_taken"), []):
        if isinstance(note, str) and "failed" in note:
            items.append(("tally", note[0].upper() + note[1:] + "."))
    if job.get("error"):
        items.append(("tally", f"Check failed: {job['error']}"))
    return items


def _color(verdict):
    return {
        "in_sync": "waveform",
        "retimed": "subtitle",
        "mislabelled": "tally",
        "mismatch": "tally",
    }.get(verdict, "neutral")


def _history(job):
    out = []
    for h in job["history"]:
        who = Mode.LABELS.get(h.get("by"), "you")
        what = (
            "removed the file"
            if h.get("removed")
            else (
                f"renamed it to {os.path.basename(h['renamed_to'])}"
                if h.get("renamed_to")
                else "applied fixes"
            )
        )
        out.append(
            {
                "text": f"{'You' if who == 'you' else who} {what}; original backed up.",
                "at": h.get("at"),
            }
        )
    return out


def _no_fix_text(job):
    verdict = job.get("verdict")
    if verdict == "in_sync":
        return "Nothing to fix: the forced subtitle lines up with this film."
    if verdict == "mismatch":
        return (
            "The forced lines don't fit this film's timing; it may belong to another cut. "
            "Talkarr won't change it. Replace the file, then check it again."
        )
    if verdict == "mislabelled":
        return (
            "This is a full subtitle saved as forced. Mislabelled files are set to be "
            "flagged only (MISLABELLED_ACTION=flag), so Talkarr leaves it alone."
        )
    if verdict == "unverifiable":
        return "Talkarr couldn't verify this subtitle either way, so it leaves it as it is."
    if job.get("status") != "completed":
        return "This subtitle hasn't been checked yet."
    return "Nothing to fix: the subtitle is clean and in sync."


@ui.route("/settings")
def settings_page():
    groups = {}
    for item in settings.configuration():
        groups.setdefault(item.group, []).append(item)
    modes = [(m, Mode.LABELS[m], MODE_TEXT[m]) for m in Mode.ALL]
    return render_template(
        "settings.html", nav="settings", modes=modes, config_groups=list(groups.items())
    )


# --- actions ------------------------------------------------------------------------------


@ui.url_value_preprocessor
def _job_must_exist(endpoint, values):
    """Actions on a job that doesn't exist are a 404, not a redirect to nowhere."""
    if (
        values
        and "job_id" in values
        and (endpoint or "").endswith("_action")
        and db.get_job(values["job_id"]) is None
    ):
        abort(404)


def _indices():
    try:
        return [int(i) for i in request.form.getlist("fix")] or None
    except ValueError:
        abort(400)


@ui.route("/job/<job_id>/apply", methods=["POST"])
def apply_action(job_id):
    overrides = {}
    for key in ("scale", "offset"):
        if request.form.get(key) not in (None, ""):
            try:
                overrides[key] = float(request.form[key])
            except ValueError:
                flash(f"{key.capitalize()} must be a number.", "error")
                return _back(url_for("ui.job_page", job_id=job_id))
    try:
        applied = review.apply_job(job_id, _indices(), overrides or None, by="you")
        done = [f["summary"] for f in applied if f["state"] == fixes.APPLIED]
        flash(f"Applied: {'; '.join(done)}." if done else "Applied.")
    except review.ReviewError as e:
        flash(f"Couldn't apply: {e}.", "error")
    return _back(url_for("ui.review_page"))


@ui.route("/job/<job_id>/dismiss", methods=["POST"])
def dismiss_action(job_id):
    try:
        review.dismiss_job(job_id, _indices())
        flash("Dismissed. It stays in Activity if you change your mind.")
    except review.ReviewError as e:
        flash(f"Couldn't dismiss: {e}.", "error")
    return _back(url_for("ui.review_page"))


@ui.route("/job/<job_id>/revert", methods=["POST"])
def revert_action(job_id):
    try:
        review.revert_job(job_id)
        flash("Restored the original subtitle. The fixes are waiting for you again.")
    except review.ReviewError as e:
        flash(f"Couldn't restore: {e}.", "error")
    return _back(url_for("ui.job_page", job_id=job_id))


@ui.route("/job/<job_id>/recheck", methods=["POST"])
def recheck_action(job_id):
    try:
        review.recheck_job(job_id)
        flash("Queued to be checked again.")
    except review.ReviewError as e:
        flash(f"Couldn't queue it: {e}.", "error")
    return _back(url_for("ui.job_page", job_id=job_id))


@ui.route("/scan", methods=["POST"])
def scan_action():
    from .app import ScanError, queue_scan

    try:
        count = queue_scan(
            request.form.get("directory") or Config.MEDIA_PATH,
            request.form.get("filter", "all"),
            requeue=bool(request.form.get("requeue")),
        )
        flash(
            f"Queued {count} subtitle{'' if count == 1 else 's'} to check."
            if count
            else "No new subtitles to check there."
        )
    except ScanError as e:
        flash(str(e), "error")
    return redirect(url_for("ui.library_page"))


@ui.route("/settings/mode", methods=["POST"])
def mode_action():
    try:
        settings.set_mode(request.form.get("mode", ""))
        flash(f"Mode is now {Mode.LABELS[settings.get_mode()]}.")
    except ValueError:
        flash("Pick one of the three modes.", "error")
    return redirect(url_for("ui.settings_page"))
