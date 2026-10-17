"""Combine the text-path and audio-path reports into one verdict.

- Either path finding a full subtitle labelled as forced wins: that's conclusive.
- The text path (full subtitle synced to the audio) is the more precise timing
  reference, so when it is conclusive it leads; the audio path confirms it, or the
  disagreement is flagged for review.
- When text can't tell (no full subtitle, or one that doesn't translate the foreign
  dialogue), the audio path decides.
- When neither can tell, the subtitle is left alone as unverifiable.
"""

from dataclasses import dataclass, field

from .verify import Verdict

CONCLUSIVE = {Verdict.IN_SYNC, Verdict.RETIMED}
# Text and audio fits agree when their corrections land cues this close together.
AGREE_SECONDS = 1.0


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    review: bool = False
    scale: float = 1.0
    offset: float = 0.0
    source: str = ""  # "text", "audio", "text+audio" or ""
    details: dict = field(default_factory=dict)

    def as_dict(self):
        d = dict(self.__dict__)
        d["verdict"] = str(self.verdict)
        return d


def combine(text=None, audio=None):
    """``text``/``audio`` are ``verify.Report`` objects (or ``None`` if not run)."""
    for name, report in (("text", text), ("audio", audio)):
        if report is not None and report.verdict == Verdict.MISLABELLED:
            return _from(report, name)

    t_ok = text is not None and text.verdict in CONCLUSIVE
    a_ok = audio is not None and audio.verdict in CONCLUSIVE

    if t_ok and a_ok:
        decision = _from(text, "text+audio")
        gap = _max_gap(text, audio)
        if gap <= AGREE_SECONDS:
            # Two independent methods, same correction: the framerate/large-offset
            # review flag exists to catch a wrong fit, which this rules out.
            decision.review = False
            decision.reason = f"{_strip_review(text.reason)}; audio agrees ({audio.reason})"
        elif _broadly_agree(text, audio, gap):
            decision.review = False
            decision.reason = (
                f"{_strip_review(text.reason)}; audio broadly agrees (same direction, cues "
                f"within {gap:.1f}s; text fit is the precise one): {audio.reason}"
            )
        else:
            decision.review = True
            decision.reason += f"; but audio disagrees ({audio.reason})"
        return decision
    if t_ok:
        if audio is not None and _corroborates(text, audio):
            # Too weak to stand alone, but the audio search independently landed on
            # the same correction: chance alignments don't pick the same framerate
            # and offset as an unrelated method.
            decision = _from(text, "text+audio")
            decision.review = False
            decision.reason = (
                f"{_strip_review(text.reason)}; audio independently finds the same "
                f"correction (scale {audio.scale:.5f}, offset {audio.offset:+.2f}s, "
                f"{audio.details.get('overlap_gain', 0):+.0%} cue time on foreign speech)"
            )
            return decision
        decision = _from(text, "text")
        if audio is not None:
            decision.reason += f"; audio couldn't confirm ({audio.reason})"
        return decision
    if a_ok:
        decision = _from(audio, "audio")
        if text is not None:
            decision.reason = f"{decision.reason}; text couldn't tell ({text.reason})"
        return decision

    if text is not None and text.verdict == Verdict.MISMATCH:
        decision = _from(text, "text")
        decision.review = True
        return decision

    reasons = [f"{name}: {r.reason}" for name, r in (("text", text), ("audio", audio)) if r]
    return Decision(
        Verdict.UNVERIFIABLE, "; ".join(reasons) or "no evidence available", review=False
    )


# How much more forced-cue time an audio fit must put on foreign speech before its
# correction counts as independent support for the text path's.
CORROBORATE_OVERLAP_GAIN = 0.2


def _corroborates(text, audio):
    """An inconclusive audio fit that moved the subtitle the same way the text path did."""
    if text.verdict != Verdict.RETIMED or audio.verdict != Verdict.UNVERIFIABLE:
        return False
    if audio.details.get("overlap_gain", 0) < CORROBORATE_OVERLAP_GAIN:
        return False
    return audio.scale == text.scale and _agree(text, audio)


def _strip_review(reason):
    return reason.replace(" (large offset or framerate change: review)", "")


def _from(report, source):
    return Decision(
        verdict=report.verdict,
        reason=report.reason,
        review=report.review,
        scale=report.scale,
        offset=report.offset,
        source=source,
    )


SAMPLE_TIMES = (600.0, 3600.0, 6000.0)
# Looser agreement: both corrections move the subtitle the same way and land cues within
# this many seconds across the film (e.g. text 25->23.976 vs audio 25->24 on Star Wars,
# where the audio fit's framerate resolution is coarser), and the text fit itself is
# precise against the synced full subtitle.
BROAD_AGREE_SECONDS = 5.0
PRECISE_TEXT_RESIDUAL = 0.25  # s; Star Wars: 0.173 against an ffsubsync-rescaled reference


def _max_gap(text, audio):
    return max(
        abs((text.scale * t + text.offset) - (audio.scale * t + audio.offset)) for t in SAMPLE_TIMES
    )


def _agree(text, audio):
    """Do both corrections put cues within AGREE_SECONDS of each other across the film?"""
    return _max_gap(text, audio) <= AGREE_SECONDS


def _broadly_agree(text, audio, gap):
    if gap > BROAD_AGREE_SECONDS or text.residual_p90 > PRECISE_TEXT_RESIDUAL:
        return False

    def direction(r, t):
        return (r.scale * t + r.offset) - t

    # The same sign of correction at every sample point (or both negligible there).
    return all(
        direction(text, t) * direction(audio, t) > 0
        or max(abs(direction(text, t)), abs(direction(audio, t))) <= AGREE_SECONDS
        for t in SAMPLE_TIMES
    )
