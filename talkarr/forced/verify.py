"""Verify an existing forced subtitle and decide what to do with it.

Pipeline (docs/PLAN.md section 3): S0 screen -> S1 text anchors -> S3 fit -> S4 verdict.
Everything here is pure: it takes cue lists and returns a ``Report``; reading
media, syncing the reference, and writing files are the caller's job.
"""

import math
from dataclasses import dataclass, field
from enum import StrEnum

from .anchors import has_foreign_annotations, text_anchors
from .fit import fit_anchors
from .subtitles import normalize

# A "forced" subtitle with this many cues is *probably* a full subtitle, but films with a
# lot of foreign dialogue have genuine forced tracks this long (Lion, Slumdog
# Millionaire, District 9: 220-400 cues). Removing one is irreversible, so a long track
# is only called mislabelled on evidence: see ``judge_long``.
MISLABEL_MIN_CUES = 200
# judge_long: share of a long track's cues that sit on foreign speech (best audio fit)
# at or above which it is a genuine forced track, and at or below which it is not.
LONG_FOREIGN_SHARE = 0.5
LONG_NOT_FOREIGN_SHARE = 0.25
# Cues per minute (over the track's own span) of ordinary dialogue; genuine forced
# tracks, even long ones, are sparser (Lion 2.8, Slumdog 3.7; Archer's full subs 19).
FULL_DIALOGUE_DENSITY = 6.0
# With a reference: share of the reference's cues the "forced" sub reproduces.
MISLABEL_REFERENCE_SHARE = 0.5
# A usable fit must explain this share of the forced cues that found any anchor,
# and at least MIN_INLIERS cues.
MIN_COVERAGE = 0.6
MIN_INLIERS = 3
# Share of (anchorable) forced lines that must be found in the reference for the
# text path to say anything; below it the reference likely doesn't translate the
# foreign dialogue, and only the audio path can tell.
MIN_FOUND_SHARE = 0.3
# A "forced" subtitle with at least this many cues that reproduces most of the
# full subtitle's cues *within its own time span* is a section of a full subtitle.
SECTION_MIN_CUES = 50
SECTION_MIN_WINDOW_CUES = 20
SECTION_SHARE = 0.5
# Retimes beyond this offset, or with a framerate change, are plausible (other
# release/cut) but are flagged for review rather than trusted blindly.
REVIEW_OFFSET = 30.0
# Lines shorter than this (normalised characters) aren't counted when judging
# whether forced lines are missing from the reference.
SPECIFIC_LINE_CHARS = 12


class Verdict(StrEnum):
    IN_SYNC = "in_sync"
    RETIMED = "retimed"
    MISLABELLED = "mislabelled"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass
class Report:
    verdict: Verdict
    reason: str
    forced_cues: int
    reference_cues: int = 0
    anchored_cues: int = 0  # forced cues with at least one candidate anchor
    inliers: int = 0
    coverage: float = 0.0
    scale: float = 1.0
    offset: float = 0.0
    residual_p90: float = 0.0
    review: bool = False  # verdict is plausible but should be confirmed before acting
    details: dict = field(default_factory=dict)

    def as_dict(self):
        d = dict(self.__dict__)
        d["verdict"] = str(self.verdict)
        return d


def judge_long(forced, reference=None, timeline_fit=None, foreign_spans=None):
    """Is a long "forced" track (``MISLABEL_MIN_CUES`` or more) a full subtitle?

    Returns a MISLABELLED report (``review`` set when the evidence is only
    circumstantial), or ``None`` when it looks like a genuine forced track for a film
    with a lot of foreign dialogue, to be verified like any other.

    ``timeline_fit``/``foreign_spans`` come from the audio path (None if not run).
    """
    n = len(forced)
    per_minute = density(forced)

    def mislabelled(reason, review):
        report = Report(Verdict.MISLABELLED, reason, n, reference_cues=len(reference or ()))
        report.review = review
        report.details["density"] = round(per_minute, 1)
        return report

    # Text: a genuine forced track is a small part of the full subtitle (its foreign
    # lines); a full subtitle reproduces most of it.
    text_share = None
    if reference:
        anchored = {a.forced_index for a in text_anchors(forced, reference)}
        text_share = len(anchored) / len(reference)
    full_density = per_minute >= FULL_DIALOGUE_DENSITY

    # Audio: does the track sit on foreign speech? That is what a forced track is for.
    # Sign language, invented languages and an English dub can all hide it, so "not on
    # foreign speech" removes on its own only when the track is as dense as dialogue
    # and the full subtitle (if any) doesn't say it's a part of it.
    if foreign_spans is not None:
        not_foreign = None
        if len(foreign_spans) < AUDIO_MIN_FOREIGN_SPANS:
            not_foreign = f"{n} cues, and the film has almost no foreign speech"
        elif timeline_fit is not None:
            share = timeline_fit.placed / n
            if share >= LONG_FOREIGN_SHARE and timeline_fit.placed > timeline_fit.chance:
                return None
            if share <= LONG_NOT_FOREIGN_SHARE:
                not_foreign = f"{n} cues, only {share:.0%} of them on foreign speech"
        if not_foreign:
            reason = f"{not_foreign}: a full subtitle labelled as forced"
            if text_share is not None and text_share < MISLABEL_REFERENCE_SHARE:
                return mislabelled(
                    f"{reason} (but only {text_share:.0%} of the full subtitle: review)", True
                )
            if not full_density:
                return mislabelled(f"{reason} ({per_minute:.1f} a minute: review)", True)
            return mislabelled(reason, review=False)

    if text_share is not None:
        if text_share < MISLABEL_REFERENCE_SHARE:
            return None
        return mislabelled(
            f"{n} cues reproducing {text_share:.0%} of the {len(reference)}-cue full "
            "subtitle: a full subtitle labelled as forced",
            review=not full_density,  # a mostly foreign film's track is both
        )

    # Neither the audio nor a full subtitle says what this is: a person decides.
    return mislabelled(
        f"{n} cues ({per_minute:.1f} a minute): probably a full subtitle labelled as "
        "forced (no audio or full-subtitle evidence: review)",
        review=True,
    )


def density(cues):
    """Cues per minute over the subtitle's own span."""
    if not cues:
        return 0.0
    return len(cues) / max((cues[-1].end - cues[0].start) / 60, 1.0)


def verify(forced, reference=None, long_ok=False):
    """Judge ``forced`` (list of Cue) against an optional full English ``reference``.

    ``long_ok``: ``judge_long`` already cleared a long track as a genuine forced one,
    so neither its length nor its overlap with the full subtitle makes it mislabelled.
    """
    n = len(forced)
    if n == 0:
        return Report(Verdict.UNVERIFIABLE, "forced subtitle has no cues", 0)

    # S0: real forced tracks are sparse; this many cues is a full subtitle.
    if n >= MISLABEL_MIN_CUES and not long_ok:
        return Report(
            Verdict.MISLABELLED,
            f"{n} cues: a full subtitle labelled as forced",
            n,
            reference_cues=len(reference or ()),
        )
    if reference is None:
        return Report(Verdict.UNVERIFIABLE, "no reference subtitle (audio path not available)", n)

    report = Report(Verdict.UNVERIFIABLE, "", n, reference_cues=len(reference))
    anchors = text_anchors(forced, reference)
    anchored = {a.forced_index for a in anchors}
    report.anchored_cues = len(anchored)

    # S0: a sparse "forced" subtitle that still reproduces most of a (short) full
    # subtitle is a copy of it.
    if (
        not long_ok
        and len(reference) >= MIN_INLIERS
        and len(anchored) >= MISLABEL_REFERENCE_SHARE * len(reference)
    ):
        report.verdict = Verdict.MISLABELLED
        report.reason = (
            f"{n} cues, {len(anchored)} of them found in the {len(reference)}-cue full "
            "subtitle: a full subtitle labelled as forced"
        )
        if density(reference) < FULL_DIALOGUE_DENSITY:
            # The "full" subtitle is as sparse as a forced track (Borat: 157 cues in 77
            # minutes), so it may be the forced track itself, saved under the wrong name.
            report.review = True
            report.reason += " (the full subtitle is sparse too: review)"
        return report

    fit = fit_anchors(anchors)
    if fit is not None:
        report.inliers = fit.inlier_count
        report.coverage = fit.inlier_count / max(1, len(anchored))
        report.scale = fit.scale
        report.offset = round(fit.offset, 3)
        report.residual_p90 = round(fit.residual_p90, 3)

    if fit is not None and fit.inlier_count >= MIN_INLIERS and report.coverage >= MIN_COVERAGE:
        section = _section_share(fit, reference)
        # (Not for a long track the audio cleared: a mostly foreign film's forced track
        # reproduces much of the full subtitle, e.g. Pan's Labyrinth.)
        if (
            not long_ok
            and n >= SECTION_MIN_CUES
            and section is not None
            and section >= SECTION_SHARE
        ):
            report.verdict = Verdict.MISLABELLED
            report.reason = (
                f"{n} cues reproducing {section:.0%} of the full subtitle within their time "
                "span: a section of a full subtitle labelled as forced"
            )
            return report
        if fit.is_identity:
            report.verdict = Verdict.IN_SYNC
            report.reason = f"{fit.inlier_count} lines line up with the full subtitle"
            return report
        report.verdict = Verdict.RETIMED
        report.reason = (
            f"{fit.inlier_count} lines line up after scale {fit.scale:.5f}, "
            f"offset {fit.offset:+.2f}s"
        )
        mapped = [fit.apply(c.start) for c in forced]
        if abs(fit.offset) > REVIEW_OFFSET or fit.scale != 1.0:
            report.review = True
            report.reason += " (large offset or framerate change: review)"
        elif min(mapped) < 0 or max(mapped) > reference[-1].end + REVIEW_OFFSET:
            report.review = True
            report.reason += " (retimed cues fall outside the film: review)"
        return report

    if has_foreign_annotations(reference):
        report.reason = "full subtitle describes foreign speech instead of translating it"
        return report

    specific = [i for i, c in enumerate(forced) if len(normalize(c.text)) >= SPECIFIC_LINE_CHARS]
    found = [i for i in specific if i in anchored]
    report.details["specific_lines"] = len(specific)
    report.details["specific_found"] = len(found)
    if len(specific) >= MIN_INLIERS and len(found) < MIN_FOUND_SHARE * len(specific):
        # Absent lines are not evidence of a wrong subtitle: many full subtitles
        # simply don't translate foreign dialogue. Only audio can decide.
        report.reason = (
            f"only {len(found)} of {len(specific)} forced lines appear in the full subtitle "
            "(it may not translate the foreign dialogue)"
        )
    elif len(anchored) >= MIN_INLIERS:
        # The lines are there, but no single offset/framerate explains them:
        # another cut (scenes added/removed) or a scrambled file. Flag, don't fix.
        best = fit.inlier_count if fit is not None else 0
        report.verdict = Verdict.MISMATCH
        report.review = True
        report.reason = (
            f"forced lines found in the full subtitle but timings disagree "
            f"(best fit explains {best}/{len(anchored)})"
        )
    else:
        report.reason = "too few forced lines to anchor on"
    return report


def _section_share(fit, reference):
    """Share of the reference cues inside the fitted forced span that are inliers."""
    times = [a.reference_time for a in fit.inliers]
    lo, hi = min(times), max(times)
    window = sum(1 for c in reference if lo <= c.start <= hi)
    if window < SECTION_MIN_WINDOW_CUES:
        return None
    return fit.inlier_count / window


# --- audio path (S2) -----------------------------------------------------------------

# Share of forced cues that must sit on foreign speech for the audio path to vouch for
# a subtitle; real forced tracks also carry signs/on-screen text, so not all will.
AUDIO_MIN_PLACED_SHARE = 0.5
# A retime must place clearly more cues than leaving the subtitle alone, or put
# clearly more of the cue time on foreign speech.
AUDIO_MIN_GAIN_SHARE = 0.2
AUDIO_MIN_OVERLAP_GAIN = 0.05
AUDIO_BIG_MOVE_OVERLAP_GAIN = 0.2
# Subtitles conventionally start ~0.1-0.2 s before speech, so the audio fit of a good
# subtitle sits a little off zero; smaller offsets than this are left alone.
AUDIO_IDENTITY_OFFSET = 0.25
# Offsets up to this (no framerate change) count as fine adjustments.
AUDIO_SMALL_MOVE = 2.0
# How far a placement must exceed the decoy baseline (share of cues, at least two).
AUDIO_CHANCE_MARGIN = 0.1
# Below this much foreign speech the film gives the audio path nothing to hold on to
# (e.g. forced subs that are all signs or sign language).
AUDIO_MIN_FOREIGN_SPANS = 3


def verify_audio(forced, timeline_fit, foreign_spans, long_ok=False):
    """Judge ``forced`` against the film's foreign-speech timeline.

    ``timeline_fit`` is ``audio.fit_timeline(forced, foreign_spans)``; ``long_ok`` as
    for ``verify``.
    """
    n = len(forced)
    report = Report(Verdict.UNVERIFIABLE, "", n)
    report.details["foreign_spans"] = len(foreign_spans)
    if n == 0:
        report.reason = "forced subtitle has no cues"
        return report
    if n >= MISLABEL_MIN_CUES and not long_ok:
        report.verdict = Verdict.MISLABELLED
        report.reason = f"{n} cues: a full subtitle labelled as forced"
        return report
    if len(foreign_spans) < AUDIO_MIN_FOREIGN_SPANS or timeline_fit is None:
        report.reason = "too little foreign speech in the audio to anchor on"
        return report

    # A placement only means something if it beats what decoy cues achieve by luck.
    beat_chance = timeline_fit.chance + max(2, math.ceil(AUDIO_CHANCE_MARGIN * n))
    need = max(MIN_INLIERS, AUDIO_MIN_PLACED_SHARE * n, beat_chance)
    report.inliers = timeline_fit.placed
    report.coverage = timeline_fit.placed / n
    report.scale = timeline_fit.scale
    report.offset = round(timeline_fit.offset, 3)
    report.details.update(
        placed_unchanged=timeline_fit.placed_identity,
        chance=timeline_fit.chance,
        overlap=timeline_fit.overlap,
        overlap_unchanged=timeline_fit.overlap_identity,
    )

    moved = timeline_fit.scale != 1.0 or abs(timeline_fit.offset) > AUDIO_IDENTITY_OFFSET
    small = timeline_fit.scale == 1.0 and abs(timeline_fit.offset) <= AUDIO_SMALL_MOVE
    gain = timeline_fit.placed - timeline_fit.placed_identity
    overlap_gain = timeline_fit.overlap / max(timeline_fit.overlap_identity, 1e-9) - 1
    report.details["overlap_gain"] = round(overlap_gain, 3)
    # Overlap (seconds of cue time on foreign speech) is the precise signal: placed
    # counts barely move for sub-second shifts or gradual framerate drift. Bigger
    # moves need proportionally stronger evidence.
    if small:
        justified = moved and overlap_gain >= AUDIO_MIN_OVERLAP_GAIN
    else:
        justified = gain >= max(2, AUDIO_MIN_GAIN_SHARE * n) or (
            gain >= 0 and overlap_gain >= AUDIO_BIG_MOVE_OVERLAP_GAIN
        )

    if justified and timeline_fit.placed >= need:
        report.verdict = Verdict.RETIMED
        report.reason = (
            f"{timeline_fit.placed}/{n} cues sit on foreign speech after scale "
            f"{timeline_fit.scale:.5f}, offset {timeline_fit.offset:+.2f}s "
            f"({timeline_fit.placed_identity} before; chance {timeline_fit.chance})"
        )
        if abs(timeline_fit.offset) > REVIEW_OFFSET or timeline_fit.scale != 1.0:
            report.review = True
            report.reason += " (large offset or framerate change: review)"
    elif timeline_fit.placed_identity >= need:
        report.verdict = Verdict.IN_SYNC
        report.reason = (
            f"{timeline_fit.placed_identity}/{n} cues already sit on foreign speech "
            f"(chance {timeline_fit.chance})"
        )
    else:
        # The film has foreign speech, but no placement beats luck: a wrong subtitle,
        # or one that is mostly signs/visual cues, or weak language ID. Can't tell.
        report.reason = (
            f"no alignment beats chance: best {timeline_fit.placed}/{n} cues, unchanged "
            f"{timeline_fit.placed_identity}, decoys {timeline_fit.chance}"
        )
    return report
