"""Eval wrapper: speech regions for the film an item belongs to (see forced.sources)."""

from ..forced import sources
from ..media import find_video_for_subtitle


def film_regions(item, cache_dir, ml_url):
    """Returns ``(regions or None, info)`` for the film the item's forced sub belongs to."""
    video = find_video_for_subtitle(item.forced)
    if video is None:
        return None, {"ok": False, "error": "no video found next to the subtitle"}
    return sources.film_regions(video, cache_dir, ml_url)
