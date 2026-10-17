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
        if _agree(text, audio):
            decision.reason += f"; audio agrees ({audio.reason})"
        else:
            decision.review = True
            decision.reason += f"; but audio disagrees ({audio.reason})"
        return decision
    if t_ok:
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


def _from(report, source):
    return Decision(
        verdict=report.verdict,
        reason=report.reason,
        review=report.review,
        scale=report.scale,
        offset=report.offset,
        source=source,
    )


def _agree(text, audio, at=(600.0, 3600.0, 6000.0)):
    """Do both corrections put cues within AGREE_SECONDS of each other across the film?"""
    return all(
        abs((text.scale * t + text.offset) - (audio.scale * t + audio.offset)) <= AGREE_SECONDS
        for t in at
    )
