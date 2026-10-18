"""Proposed fixes: what Talkarr would change about a subtitle, and doing it.

Every job ends with a *proposal*: a list of fixes plus a fingerprint of the subtitle
it was computed from. Each fix has a tier that decides which modes may apply it
without asking:

- ``safe``: reversible and corroborated (cleaning; a forced retime both checks agree on).
  Safe Auto and Full Auto apply these.
- ``confident``: one strong check, or destructive but well-founded (removing a full
  subtitle saved as forced). Full Auto applies these.
- ``uncertain``: needs a person. No mode applies these on its own.

Applying always backs up the original first, so every change can be reverted.
"""

import os
import shutil
import time

from . import settings
from .config import Config
from .db import SubType
from .fileio import atomic_write_text
from .forced import subtitles as S
from .forced.fit import SCALES
from .media import english_forced_stem

SAFE, CONFIDENT, UNCERTAIN = "safe", "confident", "uncertain"
PROPOSED, APPLIED, DISMISSED, FAILED = "proposed", "applied", "dismissed", "failed"

# scale -> (from fps, to fps); t_new = scale * t_old, so scale = to / from.
FRAMERATES = {
    1.0: None,
    25 / 23.976: ("23.976", "25"),
    23.976 / 25: ("25", "23.976"),
    24 / 23.976: ("23.976", "24"),
    23.976 / 24: ("24", "23.976"),
    25 / 24: ("24", "25"),
    24 / 25: ("25", "24"),
}
assert set(FRAMERATES) == set(SCALES)


def framerate_label(scale):
    """``0.95904`` -> ``"25 → 23.976"``; None when there is no framerate change."""
    for known, pair in FRAMERATES.items():
        if abs(known - scale) < 1e-4:
            return f"{pair[0]} → {pair[1]}" if pair else None
    return f"×{scale:.5f}"


def retime_summary(scale, offset):
    parts = []
    label = framerate_label(scale)
    if label:
        parts.append(f"{label} fps")
    if abs(offset) >= 0.005 or not parts:
        parts.append(f"{offset:+.2f} s".replace("-", "−"))
    return "Retime " + ", ".join(parts)


# --- building a proposal --------------------------------------------------------------


def fingerprint(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def build(subtitle_path, sub_type, clean=None, sync=None, decision=None):
    """Fixes for one subtitle from the pipeline's (never-written) results."""
    fixes = []
    if decision is not None and str(decision.verdict) == "mislabelled":
        action = _mislabelled_fix(subtitle_path, decision)
        # A file that is about to be removed or renamed needs no other fixes.
        return [action] if action else []

    if clean is not None and clean.success and clean.changed:
        bits = []
        if clean.ads_removed:
            bits.append(f"strip {clean.ads_removed} ad{'s' if clean.ads_removed != 1 else ''}")
        if clean.encoding_fixed:
            bits.append("repair garbled characters")
        if clean.reencoded:
            bits.append(f"{clean.source_encoding} → UTF-8")
        if clean.bom_stripped and not bits:
            bits.append("remove byte-order mark")
        fixes.append(
            _fix(
                "clean",
                "Clean: " + ", ".join(bits),
                SAFE,
                {
                    "ads": clean.ads_removed,
                    "encoding": clean.source_encoding,
                },
            )
        )

    if sync is not None and sub_type != SubType.FORCED and getattr(sync, "success", False):
        offset = sync.offset or 0.0
        framerate = getattr(sync, "framerate", None) or 1.0
        if abs(offset) >= Config.SYNC_MIN_OFFSET or abs(framerate - 1.0) > 1e-3:
            confidence = sync.confidence or 0.0
            tier = CONFIDENT if confidence >= Config.SYNC_MIN_CONFIDENCE else UNCERTAIN
            summary = f"Sync {offset:+.2f} s".replace("-", "−")
            fixes.append(
                _fix(
                    "sync",
                    summary,
                    tier,
                    {
                        "offset": offset,
                        "framerate": framerate,
                        "confidence": confidence,
                    },
                )
            )

    if decision is not None and str(decision.verdict) == "retimed":
        if decision.review:
            tier = UNCERTAIN
        elif decision.source == "text+audio":
            tier = SAFE
        else:
            tier = CONFIDENT
        fixes.append(
            _fix(
                "retime",
                retime_summary(decision.scale, decision.offset),
                tier,
                {
                    "scale": decision.scale,
                    "offset": decision.offset,
                },
            )
        )
    return fixes


def _mislabelled_fix(path, decision):
    action = settings.mislabelled_action()
    if action == "flag":
        return None
    target = _regular_name(path)
    if action == "rename_or_remove_and_backup" and target and not os.path.exists(target):
        return _fix(
            "rename",
            f"Rename to {os.path.basename(target)} (backup kept): it is a full subtitle",
            CONFIDENT,
            {"to": target},
        )
    backup = action != "remove"
    return _fix(
        "remove",
        f"Remove ({'backup kept' if backup else 'no backup'}): a full subtitle saved as forced",
        CONFIDENT,
        {"reason": decision.reason, "backup": backup},
    )


def _regular_name(path):
    directory, name = os.path.split(path)
    stem = english_forced_stem(name)
    return os.path.join(directory, f"{stem}.en.srt") if stem else None


def _fix(kind, summary, tier, params):
    return {
        "kind": kind,
        "summary": summary,
        "tier": tier,
        "params": params,
        "state": PROPOSED,
        "error": None,
        "result": None,  # what applying it did, in words (e.g. "Blacklisted in Bazarr ...")
    }


# --- policy -----------------------------------------------------------------------------


def auto_indices(mode, fixes):
    """Which fixes the given mode applies without asking."""
    allowed = {
        settings.Mode.MANUAL: set(),
        settings.Mode.SAFE_AUTO: {SAFE},
        settings.Mode.FULL_AUTO: {SAFE, CONFIDENT},
    }[mode]
    return [i for i, f in enumerate(fixes) if f["state"] == PROPOSED and f["tier"] in allowed]


def outcome(fixes):
    states = [f["state"] for f in fixes]
    if not states:
        return "nothing"
    if FAILED in states:
        return "failed"
    if all(s == DISMISSED for s in states):
        return "dismissed"
    if all(s in (APPLIED, DISMISSED) for s in states):
        return "applied"
    if APPLIED in states:
        return "partial"
    return "pending"


# --- applying --------------------------------------------------------------------------

ORDER = {"clean": 0, "sync": 1, "retime": 1, "rename": 2, "remove": 2}


class ApplyError(Exception):
    pass


def apply(job, proposal, indices, overrides=None, sync_runner=None):
    """Apply ``proposal["fixes"][i]`` for each index. Returns ``(fixes, backup)``.

    ``overrides`` may carry ``{"scale": .., "offset": ..}`` for a retime (or a sync,
    which then becomes a plain offset/framerate retime). Raises ``ApplyError`` if the
    subtitle changed since it was checked; individual fix failures are recorded on
    the fix instead.
    """
    path = job["subtitle_path"]
    fixes = [dict(f) for f in proposal["fixes"]]
    if not os.path.isfile(path):
        raise ApplyError("the subtitle file no longer exists")
    if proposal.get("fingerprint") and fingerprint(path) != proposal["fingerprint"]:
        raise ApplyError("the subtitle changed since it was checked; check it again first")

    chosen = [fixes[i] for i in indices if fixes[i]["state"] == PROPOSED]
    needs_copy = any(f["kind"] != "remove" or f["params"].get("backup", True) for f in chosen)
    backup = _backup(job) if needs_copy else _no_backup(job)
    for i in sorted(indices, key=lambda i: ORDER.get(fixes[i]["kind"], 9)):
        fix = fixes[i]
        if fix["state"] != PROPOSED:
            continue
        try:
            _apply_one(fix, job, path, overrides or {}, backup, sync_runner)
            fix["state"], fix["error"] = APPLIED, None
        except Exception as e:  # noqa: BLE001 - recorded on the fix, shown in the UI
            fix["state"], fix["error"] = FAILED, str(e)
            break
        if fix["kind"] in ("rename", "remove"):
            break
    return fixes, backup


def _apply_one(fix, job, path, overrides, backup, sync_runner):
    kind = fix["kind"]
    if kind == "clean":
        from .cleaner import clean_subtitle

        result = clean_subtitle(path, write=True)
        if not result.success:
            raise ApplyError(result.error)
    elif kind in ("retime", "sync") and ("scale" in overrides or "offset" in overrides):
        scale = float(overrides.get("scale", fix["params"].get("scale", 1.0)))
        offset = float(overrides.get("offset", fix["params"].get("offset", 0.0)))
        _retime(path, scale, offset)
        fix["params"] = {**fix["params"], "scale": scale, "offset": offset, "adjusted": True}
        fix["summary"] = retime_summary(scale, offset)
    elif kind == "retime":
        _retime(path, fix["params"]["scale"], fix["params"]["offset"])
    elif kind == "sync":
        from .sync import sync_subtitle

        result = (sync_runner or sync_subtitle)(path, job.get("media_path"), SubType.REGULAR)
        if not result.success:
            raise ApplyError(result.error)
    elif kind == "rename":
        target = fix["params"]["to"]
        if os.path.exists(target):
            raise ApplyError(f"{os.path.basename(target)} already exists")
        os.rename(path, target)
        backup["renamed_to"] = target
    elif kind == "remove":
        fix["result"] = _remove(path, job, backup)
        backup["removed"] = True
    else:
        raise ApplyError(f"unknown fix: {kind}")


def _remove(path, job, backup):
    """Delete a subtitle; through Bazarr's blacklist when Bazarr downloaded it."""
    from . import bazarr
    from .media import find_video_for_subtitle

    if bazarr.enabled():
        client = bazarr.Bazarr()
        video = job.get("media_path") or find_video_for_subtitle(path)
        try:
            download = client.find_download(path, video)
            if download is not None:
                client.blacklist(download)
                if os.path.exists(path):  # Bazarr said yes but left it; finish the job
                    os.remove(path)
                backup["bazarr"] = {"provider": download.provider, "subs_id": download.subs_id}
                return (
                    f"Blacklisted in Bazarr ({download.provider} #{download.subs_id}); "
                    "Bazarr is searching for a different one."
                )
        except bazarr.BazarrError as e:
            # Deleting it anyway would let Bazarr fetch the same file again.
            raise ApplyError(
                f"couldn't reach Bazarr to blacklist it, so it was left in place ({e})"
            ) from e
        os.remove(path)
        return "Bazarr has no record of downloading it, so it was deleted directly."
    os.remove(path)
    return "Deleted."


def _retime(path, scale, offset):
    cues = S.load(path)
    atomic_write_text(path, S.compose(S.transform(cues, scale=scale, offset=offset)))


def _backup(job):
    path = job["subtitle_path"]
    directory = os.path.join(Config.BACKUP_DIR, job["id"], time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(directory, exist_ok=True)
    copy = os.path.join(directory, os.path.basename(path))
    shutil.copy2(path, copy)
    return {"path": path, "copy": copy, "renamed_to": None, "removed": False, "at": time.time()}


def _no_backup(job):
    return {
        "path": job["subtitle_path"],
        "copy": None,
        "renamed_to": None,
        "removed": False,
        "at": time.time(),
    }


def revert(job, backup):
    """Put the original subtitle back exactly as it was before the fixes."""
    if backup and backup.get("removed") and not backup.get("copy"):
        raise ApplyError("it was removed without a backup, so it can't be restored")
    if not backup or not os.path.isfile(backup.get("copy") or ""):
        raise ApplyError("no backup to restore")
    renamed = backup.get("renamed_to")
    if renamed and os.path.exists(renamed):
        os.remove(renamed)
    shutil.copy2(backup["copy"], backup["path"])
