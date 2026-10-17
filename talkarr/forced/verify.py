"""Verify an existing forced subtitle and decide what to do with it.

Pipeline (docs/PLAN.md section 3): S0 screen -> S1 text anchors -> S3 fit -> S4 verdict.
Everything here is pure: it takes cue lists and returns a ``Report``; reading
media, syncing the reference, and writing files are the caller's job.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from .anchors import has_foreign_annotations, text_anchors
from .fit import fit_anchors
from .subtitles import normalize

# A "forced" subtitle with this many cues, and no reference to compare with,
# is a full subtitle (real forced tracks rarely exceed ~100).
MISLABEL_MIN_CUES = 200
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


def verify(forced, reference=None):
    """Judge ``forced`` (list of Cue) against an optional full English ``reference``."""
    n = len(forced)
    if n == 0:
        return Report(Verdict.UNVERIFIABLE, "forced subtitle has no cues", 0)

    # S0: real forced tracks are sparse; this many cues is a full subtitle.
    if n >= MISLABEL_MIN_CUES:
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
    if len(reference) >= MIN_INLIERS and len(anchored) >= MISLABEL_REFERENCE_SHARE * len(reference):
        report.verdict = Verdict.MISLABELLED
        report.reason = (
            f"{n} cues, {len(anchored)} of them found in the {len(reference)}-cue full "
            "subtitle: a full subtitle labelled as forced"
        )
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
        if n >= SECTION_MIN_CUES and section is not None and section >= SECTION_SHARE:
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
