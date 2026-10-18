"""Verify one forced subtitle file end to end (used by the worker).

Reads only. Gathers the evidence (full subtitle synced to the audio, film speech
analysis from talkarr-ml), runs both verification paths, and combines them. Caches
live under ``cache_dir`` so a second look at the same film is cheap.
"""

import logging
import os

from ..media import find_reference_subtitle
from . import sources
from . import subtitles as S
from .audio import fit_timeline, foreign_timeline
from .decide import combine
from .verify import MISLABEL_MIN_CUES, verify, verify_audio

log = logging.getLogger(__name__)


def verify_forced(forced_path, video, cache_dir, ml_url=""):
    """Returns ``(Decision, details)`` for the forced subtitle at ``forced_path``."""
    forced = S.load(forced_path)
    details = {"forced_cues": len(forced)}

    # A "forced" subtitle with this many cues is a full subtitle; the count alone
    # proves it, so skip the expensive evidence (ffsubsync pass, audio analysis).
    if len(forced) >= MISLABEL_MIN_CUES:
        decision = combine(verify(forced, None))
        details["ribbon"] = {
            "runtime": round(max([c.end for c in forced] + [0.0]) + 60.0, 1),
            "cues": [[round(c.start, 3), round(c.end, 3)] for c in forced],
            "speech": [],
        }
        return decision, details

    text_report = None
    reference_path = find_reference_subtitle(forced_path)
    details["reference"] = reference_path
    if reference_path:
        reference, sync_info = sources.synced_reference(
            video, reference_path, os.path.join(cache_dir, "refsync")
        )
        details["reference_sync"] = sync_info
        if reference is None:
            # ffsubsync failed: an unsynced reference still catches mislabels and
            # lines that are missing, but its timing is not trusted (review).
            reference = S.load(reference_path)
        text_report = verify(forced, reference)
        if not sync_info.get("ok") and text_report.verdict == "retimed":
            text_report.review = True
            text_report.reason += " (full subtitle could not be synced to the audio: review)"
        details["text"] = text_report.as_dict()

    audio_report = None
    speech, runtime = [], None
    if ml_url and len(forced) > 0:
        try:
            regions, info = sources.film_regions(video, os.path.join(cache_dir, "audio"), ml_url)
        except Exception as e:  # noqa: BLE001 - audio is optional evidence
            log.warning("audio analysis failed for %s: %s", video, e)
            regions, info = None, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        details["audio_analysis"] = info
        if regions is not None:
            runtime = info.get("duration")
            timeline = foreign_timeline(regions)
            speech = [[round(a, 2), round(b, 2)] for a, b in timeline.foreign]
            fit = fit_timeline(forced, timeline.foreign)
            audio_report = verify_audio(forced, fit, timeline.foreign)
            details["audio"] = audio_report.as_dict()

    decision = combine(text_report, audio_report)
    # What the review page draws: the cues, the film's length, the foreign speech.
    ends = [c.end for c in forced]
    details["ribbon"] = {
        "runtime": round(runtime or (max(ends + [0.0]) + 60.0), 1),
        "cues": [[round(c.start, 3), round(c.end, 3)] for c in forced],
        "speech": speech,
    }
    return decision, details
