"""Audio-path evaluation: foreign-speech timelines for corpus films (cached).

Extraction (ffmpeg, on Steel) and analysis (talkarr-ml, on eezo) run once per film
and audio stream; results are cached as JSON so that tuning the timeline and fit
thresholds re-runs in seconds.
"""

import hashlib
import json
import os
import tempfile

from ..forced import audio
from ..media import find_video_for_subtitle

ANALYSIS_VERSION = "1"  # bump when extraction or the talkarr-ml contract changes


def film_regions(item, cache_dir, ml_url):
    """Returns ``(regions or None, info)`` for the film the item's forced sub belongs to."""
    video = find_video_for_subtitle(item.forced)
    if video is None:
        return None, {"ok": False, "error": "no video found next to the subtitle"}
    streams = audio.probe_audio_streams(video)
    stream = audio.choose_audio_stream(streams)
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
        with tempfile.TemporaryDirectory() as tmp:
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
