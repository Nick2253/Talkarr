"""Sync a full reference subtitle to its video (ffsubsync) into a cache.

The text path is only as good as the reference's timing, so before a forced
subtitle is judged "retimed" or "in sync", the full subtitle it is compared with
is aligned to the audio. Results are cached on disk keyed by file identity, so
re-running a sweep doesn't repeat the expensive (~1-2 min/film) audio pass.
The library is never written to.
"""

import hashlib
import json
import os

from ..forced import subtitles as S
from ..media import find_video_for_subtitle
from ..sync import run_ffsubsync

TIMEOUT = 1200


def synced_reference(item, cache_dir):
    """Returns ``(cues or None, info dict)`` for the item's reference subtitle."""
    video = find_video_for_subtitle(item.forced)
    if video is None:
        return None, {"ok": False, "error": "no video found next to the subtitle"}

    key = _cache_key(video, item.reference)
    os.makedirs(cache_dir, exist_ok=True)
    srt_path = os.path.join(cache_dir, f"{key}.srt")
    info_path = os.path.join(cache_dir, f"{key}.json")

    if os.path.exists(info_path):
        with open(info_path) as f:
            info = json.load(f)
    else:
        result = run_ffsubsync(item.reference, video, srt_path, timeout=TIMEOUT)
        info = {
            "ok": result.success,
            "offset": result.offset,
            "framerate": result.framerate,
            "score": result.score,
            "error": result.error,
            "video": video,
        }
        with open(info_path, "w") as f:
            json.dump(info, f)

    if not info["ok"]:
        return None, info
    return S.load(srt_path), info


def _cache_key(*paths):
    h = hashlib.sha256()
    for path in paths:
        st = os.stat(path)
        h.update(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    return h.hexdigest()[:24]
