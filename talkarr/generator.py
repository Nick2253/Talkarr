import logging
import os

import requests

from .config import Config

log = logging.getLogger(__name__)


class GenerateResult:
    def __init__(self, success, output_path=None, cue_count=0, error=None):
        self.success = success
        self.output_path = output_path
        self.cue_count = cue_count
        self.error = error


def generate_forced_subtitle(media_path, output_path=None, primary_language="en"):
    """Generate a forced subtitle by detecting non-primary-language speech
    via Whisper and translating it to English.

    Requires WHISPER_URL to be set.
    """
    if not Config.WHISPER_URL:
        return GenerateResult(success=False,
                              error="WHISPER_URL not configured")

    if not os.path.isfile(media_path):
        return GenerateResult(success=False,
                              error=f"Media not found: {media_path}")

    if output_path is None:
        base = os.path.splitext(media_path)[0]
        output_path = f"{base}.en.forced.srt"

    try:
        segments = _transcribe_with_language_detection(media_path)
        foreign_segments = [
            s for s in segments
            if s.get("language", primary_language) != primary_language
            and s.get("no_speech_prob", 1.0) < 0.5
        ]

        if not foreign_segments:
            log.info("No foreign language segments detected in %s",
                     os.path.basename(media_path))
            return GenerateResult(success=True, cue_count=0)

        translated = _translate_segments(media_path, foreign_segments)

        if not translated:
            return GenerateResult(success=True, cue_count=0)

        _write_srt(translated, output_path)

        log.info("Generated forced subtitle: %s (%d cues)",
                 os.path.basename(output_path), len(translated))

        return GenerateResult(
            success=True,
            output_path=output_path,
            cue_count=len(translated),
        )

    except Exception as e:
        log.exception("Failed to generate forced subtitle for %s", media_path)
        return GenerateResult(success=False, error=str(e))


def _transcribe_with_language_detection(media_path):
    """Transcribe audio with per-segment language detection via Whisper API."""
    url = Config.WHISPER_URL.rstrip("/")

    with open(media_path, "rb") as f:
        resp = requests.post(
            f"{url}/audio/transcriptions",
            files={"file": (os.path.basename(media_path), f)},
            data={
                "model": Config.WHISPER_MODEL,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
            timeout=1800,
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("segments", [])


def _translate_segments(media_path, segments):
    """Translate foreign segments to English.

    For now, re-transcribes the full file with task=translate and filters
    to only the time ranges that contained foreign speech.
    """
    url = Config.WHISPER_URL.rstrip("/")

    with open(media_path, "rb") as f:
        resp = requests.post(
            f"{url}/audio/translations",
            files={"file": (os.path.basename(media_path), f)},
            data={
                "model": Config.WHISPER_MODEL,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
            timeout=1800,
        )
    resp.raise_for_status()
    data = resp.json()

    translated_segments = data.get("segments", [])

    foreign_ranges = [(s["start"], s["end"]) for s in segments]
    matched = []
    for seg in translated_segments:
        seg_mid = (seg["start"] + seg["end"]) / 2
        for start, end in foreign_ranges:
            if start - 1.0 <= seg_mid <= end + 1.0:
                matched.append(seg)
                break

    return matched


def _write_srt(segments, output_path):
    """Write segments as an SRT file."""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = _format_srt_time(seg["start"])
            end = _format_srt_time(seg["end"])
            text = seg.get("text", "").strip()
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def _format_srt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
