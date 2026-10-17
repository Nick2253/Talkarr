"""Eval wrapper: the item's full subtitle synced to its video (see forced.sources)."""

from ..forced import sources
from ..media import find_video_for_subtitle


def synced_reference(item, cache_dir):
    """Returns ``(cues or None, info dict)`` for the item's reference subtitle."""
    video = find_video_for_subtitle(item.forced)
    if video is None:
        return None, {"ok": False, "error": "no video found next to the subtitle"}
    return sources.synced_reference(video, item.reference, cache_dir)
