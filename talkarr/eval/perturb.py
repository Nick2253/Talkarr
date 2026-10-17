"""Known breakages applied to a good forced subtitle, with the expected verdict."""

import random
from dataclasses import dataclass

from ..forced import subtitles as S
from ..forced.subtitles import Cue
from ..forced.verify import Verdict


@dataclass(frozen=True)
class Case:
    name: str
    cues: list
    expected: Verdict
    # Other verdicts that also count as a pass (e.g. "can't tell" for a wrong sub).
    also_ok: tuple = ()
    # For retime cases: the transform that broke it (to check the repair).
    scale: float = 1.0
    offset: float = 0.0


def cases(forced, reference, other_forced=None, seed=0):
    rng = random.Random(seed)
    out = [Case("original", forced, Verdict.IN_SYNC)]
    for offset in (0.5, -0.5, 2.0, -2.0, 10.0, -60.0):
        out.append(_retime(f"shift {offset:+g}s", forced, 1.0, offset))
    for name, scale in (
        ("pal 23.976->25", 23.976 / 25),
        ("pal 25->23.976", 25 / 23.976),
        ("24->23.976", 24 / 23.976),
    ):
        out.append(_retime(name, forced, scale, 0.0))
        out.append(_retime(f"{name} +2s", forced, scale, 2.0))

    keep = sorted(rng.sample(range(len(forced)), max(3, int(len(forced) * 0.6))))
    out.append(Case("40% of cues dropped", [forced[i] for i in keep], Verdict.IN_SYNC))

    junk = [Cue(t, t + 2.0, "Lorem ipsum dolor sit amet consectetur") for t in (5.0, 900.0, 4000.0)]
    out.append(
        Case("3 junk cues added", sorted(forced + junk, key=lambda c: c.start), Verdict.IN_SYNC)
    )

    out.append(Case("full subtitle labelled forced", reference, Verdict.MISLABELLED))
    if other_forced:
        # The text path can't prove a subtitle is wrong (only that its lines are
        # missing); what matters is that it is never accepted or "fixed".
        out.append(
            Case(
                "another film's forced sub",
                other_forced,
                Verdict.MISMATCH,
                also_ok=(Verdict.UNVERIFIABLE,),
            )
        )
    return out


def _retime(name, forced, scale, offset):
    broken = S.transform(forced, scale=scale, offset=offset)
    return Case(name, broken, Verdict.RETIMED, scale=scale, offset=offset)


def repair_error(case, report, original):
    """Per-cue start-time error (s) after applying the reported fit to the broken cues."""
    repaired = S.transform(case.cues, scale=report.scale, offset=report.offset)
    return [
        abs(r.start - o.start)
        for r, o in zip(repaired, original, strict=True)
        # Cues the breakage pushed before 0:00 were clamped and can't be recovered.
        if case.scale * o.start + case.offset > 0
    ]
