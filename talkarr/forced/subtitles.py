"""Minimal subtitle model used by the forced-subtitle pipeline.

Times are float seconds. Cues are immutable; transforms return new lists.
"""

import re
import unicodedata
from dataclasses import dataclass, replace

import pysrt

from ..cleaner import UTF8_BOM, decode_subtitle

_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}")
_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Dialogue dashes and speaker labels at line start: "- RAMIUS: Very well."
_SPEAKER_RE = re.compile(r"^\s*(?:[-–—]\s*)?(?:[A-Z][A-Z .']{1,20}:\s*)?", re.MULTILINE)
_NON_WORD_RE = re.compile(r"[^\w\s]")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self):
        return self.end - self.start

    @property
    def midpoint(self):
        return (self.start + self.end) / 2


def load(path):
    """Read an SRT file in any encoding we can decode."""
    with open(path, "rb") as f:
        raw = f.read()
    decoded = decode_subtitle(raw.removeprefix(UTF8_BOM))
    if decoded is None:
        raise ValueError(f"cannot decode {path}")
    return parse(decoded[0])


def parse(text):
    return [
        Cue(_seconds(item.start), _seconds(item.end), item.text) for item in pysrt.from_string(text)
    ]


def compose(cues):
    """Render cues as SRT text."""
    blocks = []
    for i, cue in enumerate(cues, 1):
        blocks.append(f"{i}\n{_timestamp(cue.start)} --> {_timestamp(cue.end)}\n{cue.text}\n")
    return "\n".join(blocks)


def transform(cues, scale=1.0, offset=0.0):
    """Map every cue time t to ``scale * t + offset``; negative times clamp to 0."""
    return [
        replace(
            cue, start=max(0.0, scale * cue.start + offset), end=max(0.0, scale * cue.end + offset)
        )
        for cue in cues
    ]


def normalize(text):
    """Text reduced to comparable words: no tags, sound descriptions, speaker
    labels, punctuation, case, or accents/quote styles."""
    text = _TAG_RE.sub(" ", text)
    text = _BRACKET_RE.sub(" ", text)
    text = _SPEAKER_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NON_WORD_RE.sub(" ", text.lower())
    return _SPACE_RE.sub(" ", text).strip()


def _seconds(t):
    return t.ordinal / 1000.0


def _timestamp(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
