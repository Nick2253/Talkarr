"""Find forced subtitles in a media library and the full subtitle beside each."""

import os
from dataclasses import dataclass

SUB_EXT = ".srt"
FORCED_TAGS = (".forced", ".foreign")
ENGLISH_TAGS = (".en", ".eng", ".english")
# Preference order for the full English reference subtitle.
REFERENCE_SUFFIXES = (
    ".en.srt",
    ".eng.srt",
    ".english.srt",
    ".en.sdh.srt",
    ".en.hi.srt",
    ".en.cc.srt",
    ".eng.sdh.srt",
    ".eng.hi.srt",
)


@dataclass(frozen=True)
class Item:
    title: str  # folder name, e.g. "The Hunt for Red October (1990)"
    forced: str
    reference: str | None


def find_items(root):
    """Every English forced subtitle under ``root``, with its full-subtitle sibling."""
    items = []
    for directory, _, files in os.walk(root):
        names = set(files)
        for name in sorted(files):
            stem = _english_forced_stem(name)
            if stem is None:
                continue
            reference = next(
                (
                    os.path.join(directory, stem + s)
                    for s in REFERENCE_SUFFIXES
                    if stem + s in names
                ),
                None,
            )
            items.append(
                Item(os.path.basename(directory), os.path.join(directory, name), reference)
            )
    return items


def _english_forced_stem(name):
    """``Movie.en.forced.srt`` -> ``Movie``; ``None`` if not an English forced sub."""
    lower = name.lower()
    if not lower.endswith(SUB_EXT):
        return None
    base = lower[: -len(SUB_EXT)]
    for forced in FORCED_TAGS:
        if base.endswith(forced):
            rest = base[: -len(forced)]
            for lang in ENGLISH_TAGS:
                if rest.endswith(lang):
                    return name[: len(rest) - len(lang)]
    return None
