"""Text anchors: where each forced cue's line appears in a full English subtitle.

A full subtitle usually contains the forced lines too (the forced track is the
subset shown to everyone). Matching by text tells us when each forced line is
spoken *according to the full subtitle*, which is VAD-syncable and therefore a
trustworthy timing reference once synced.

Matching is deliberately permissive (several candidates per forced cue): short
lines like "It's time." repeat, and the robust fit in ``fit.py`` picks the
consistent set.
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .subtitles import normalize

MIN_SCORE = 80
MAX_CANDIDATES = 20
# Below this many characters a line is too generic to anchor on ("Yes.", "Go!").
MIN_CHARS = 4

# Full subtitles that describe foreign speech instead of translating it can't
# confirm a forced subtitle's lines.
FOREIGN_ANNOTATION_RE = re.compile(
    r"[\[(]\s*(speaking|speaks|in|continues in|conversing in|chattering in|"
    r"singing in)\s+[A-Z]?[a-z]+\s*[\])]|[\[(]\s*foreign (language|dialogue)\s*[\])]|"
    r"[\[(]\s*(non-english|indistinct foreign)[^\])]*[\])]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Anchor:
    forced_index: int
    forced_time: float
    reference_time: float
    score: float


def text_anchors(forced, reference):
    """Candidate anchors for each forced cue against the reference cues."""
    targets = _reference_targets(reference)
    anchors = []
    for i, cue in enumerate(forced):
        text = normalize(cue.text)
        if len(text) < MIN_CHARS:
            continue
        scored = []
        for ref_text, ref_time in targets:
            score = _similarity(text, ref_text)
            if score >= MIN_SCORE:
                scored.append((score, ref_time))
        scored.sort(reverse=True)
        for score, ref_time in scored[:MAX_CANDIDATES]:
            anchors.append(Anchor(i, cue.start, ref_time, score / 100.0))
    return anchors


def has_foreign_annotations(cues, minimum=2):
    """True if the subtitle describes foreign speech ("[speaking Russian]")."""
    return sum(1 for c in cues if FOREIGN_ANNOTATION_RE.search(c.text)) >= minimum


def _reference_targets(reference):
    """Normalized reference texts with start times, including each cue joined with
    the next one, so a forced line split across two full-subtitle cues still matches."""
    normalized = [normalize(c.text) for c in reference]
    targets = []
    for j, cue in enumerate(reference):
        if normalized[j]:
            targets.append((normalized[j], cue.start))
        if j + 1 < len(reference) and reference[j + 1].start - cue.end < 1.0:
            joined = f"{normalized[j]} {normalized[j + 1]}".strip()
            if joined:
                targets.append((joined, cue.start))
    return targets


def _similarity(a, b):
    score = max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))
    # One side containing the other ("cold this morning captain" in "its cold
    # this morning captain") only counts when both are long enough to be specific.
    if min(len(a), len(b)) >= 12:
        score = max(score, fuzz.partial_ratio(a, b) - 5)
    return score
