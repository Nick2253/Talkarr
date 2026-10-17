"""Evidence sources for forced-subtitle verification, with on-disk caches.

- ``synced_reference``: the full English subtitle aligned to the video with ffsubsync
  (a temp copy in the cache; the library file is never touched).
- ``film_regions``: the film's speech regions with language scores, from talkarr-ml.

Both are expensive (minutes per film) and cached by file identity, so re-checking a
film, or re-running with tuned thresholds, is cheap.
"""

import hashlib
import json
import os
import tempfile

from ..sync import run_ffsubsync
from . import audio
from . import subtitles as S

REFSYNC_TIMEOUT = 1200
ANALYSIS_VERSION = "1"  # bump when extraction or the talkarr-ml contract changes


def synced_reference(video, reference, cache_dir):
    """Returns ``(cues or None, info dict)`` for ``reference`` synced to ``video``."""
    key = _cache_key(video, reference)
    os.makedirs(cache_dir, exist_ok=True)
    srt_path = os.path.join(cache_dir, f"{key}.srt")
    info_path = os.path.join(cache_dir, f"{key}.json")

    info = None
    if os.path.exists(info_path):
        with open(info_path) as f:
            info = json.load(f)
        if not info.get("ok"):
            info = None  # failures may be environmental (fixed since); retry them
    if info is None:
        result = run_ffsubsync(reference, video, srt_path, timeout=REFSYNC_TIMEOUT)
        info = {
            "ok": result.success,
            "offset": result.offset,
            "framerate": result.framerate,
            "score": result.score,
            "error": result.error,
        }
        with open(info_path, "w") as f:
            json.dump(info, f)

    if not info["ok"]:
        return None, info
    return S.load(srt_path), info


def film_regions(video, cache_dir, ml_url):
    """Returns ``(regions or None, info)`` for the video's dialogue track."""
    stream = audio.choose_audio_stream(audio.probe_audio_streams(video))
    if stream is None:
        return None, {"ok": False, "error": "no usable audio stream"}

    st = os.stat(video)
    key = hashlib.sha256(
        f"{video}\0{st.st_size}\0{st.st_mtime_ns}\0{stream.index}\0{ANALYSIS_VERSION}".encode()
    ).hexdigest()[:24]
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    else:
        with tempfile.TemporaryDirectory(dir=cache_dir) as tmp:
            clip = audio.extract_dialogue(video, stream, os.path.join(tmp, "dialogue.opus"))
            data = audio.analyze(clip, ml_url)
        data["stream"] = {
            "index": stream.index,
            "layout": stream.layout,
            "center": stream.has_center,
            "language": stream.language,
        }
        with open(path, "w") as f:
            json.dump(data, f)

    info = {
        "ok": True,
        "stream": data["stream"],
        "duration": data.get("duration"),
        "regions": len(data["regions"]),
    }
    return audio.regions_from_response(data), info


def _cache_key(*paths):
    h = hashlib.sha256()
    for path in paths:
        st = os.stat(path)
        h.update(f"{path}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    return h.hexdigest()[:24]
