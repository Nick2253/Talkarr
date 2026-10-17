"""Find forced subtitles in a media library and the full subtitle beside each."""

import os
from dataclasses import dataclass

from ..media import english_forced_stem, find_reference_subtitle


@dataclass(frozen=True)
class Item:
    title: str  # folder name, e.g. "The Hunt for Red October (1990)"
    forced: str
    reference: str | None


def find_items(root):
    """Every English forced subtitle under ``root``, with its full-subtitle sibling."""
    items = []
    for directory, _, files in os.walk(root):
        for name in sorted(files):
            if english_forced_stem(name) is None:
                continue
            path = os.path.join(directory, name)
            items.append(Item(os.path.basename(directory), path, find_reference_subtitle(path)))
    return items
