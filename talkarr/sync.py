import logging
import os
import re
import subprocess
import tempfile

from .db import SubType
from .fileio import replace_preserving_metadata

log = logging.getLogger(__name__)

OFFSET_RE = re.compile(r"offset seconds:\s*([-\d.]+)", re.IGNORECASE)
# ffsubsync >= 0.4 logs "framerate scale factor: 1.000".
FRAMERATE_RE = re.compile(r"framerate scale factor:\s*([\d.]+)", re.IGNORECASE)
SCORE_RE = re.compile(r"\bscore:\s*([-\d.]+)", re.IGNORECASE)


class SyncResult:
    def __init__(
        self,
        success,
        offset=None,
        confidence=None,
        output_path=None,
        error=None,
        skipped=False,
        framerate=None,
        score=None,
    ):
        self.success = success
        self.framerate = framerate
        self.score = score
        self.offset = offset
        self.confidence = confidence
        self.output_path = output_path
        self.error = error
        self.skipped = skipped


def sync_subtitle(subtitle_path, media_path, sub_type, max_offset=120):
    """Sync a subtitle against its media file.

    Regular/HI subs get full VAD-based sync via ffsubsync.
    Forced subs are skipped (sparse cues would be destroyed by VAD).
    """
    if sub_type == SubType.FORCED:
        log.info("Skipping sync for forced subtitle: %s", os.path.basename(subtitle_path))
        return SyncResult(success=True, skipped=True)

    if not os.path.isfile(subtitle_path):
        return SyncResult(success=False, error=f"Subtitle not found: {subtitle_path}")

    if not os.path.isfile(media_path):
        return SyncResult(success=False, error=f"Media not found: {media_path}")

    fd, output_path = tempfile.mkstemp(suffix=".srt", dir=os.path.dirname(subtitle_path))
    os.close(fd)

    try:
        cmd = [
            "ffsubsync",
            media_path,
            "-i",
            subtitle_path,
            "-o",
            output_path,
            "--max-offset-seconds",
            str(max_offset),
        ]
        log.info(
            "Running ffsubsync: %s → %s",
            os.path.basename(subtitle_path),
            os.path.basename(media_path),
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

        output = result.stderr + result.stdout
        offset = _parse_offset(output)
        framerate = _parse_framerate(output)
        score = _parse_score(output)

        if result.returncode != 0:
            os.unlink(output_path)
            return SyncResult(
                success=False,
                error=f"ffsubsync exit {result.returncode}: {result.stderr[:500]}",
            )

        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            if os.path.exists(output_path):
                os.unlink(output_path)
            return SyncResult(success=False, error="ffsubsync produced empty output")

        confidence = _estimate_confidence(offset, framerate)

        replace_preserving_metadata(output_path, subtitle_path)
        log.info("Sync complete: offset=%.2fs, confidence=%.2f", offset or 0, confidence)

        return SyncResult(
            success=True,
            offset=offset,
            confidence=confidence,
            output_path=subtitle_path,
            framerate=framerate,
            score=score,
        )

    except subprocess.TimeoutExpired:
        if os.path.exists(output_path):
            os.unlink(output_path)
        return SyncResult(success=False, error="ffsubsync timed out (600s)")

    except Exception as e:  # noqa: BLE001 - reported on the job, never fatal
        if os.path.exists(output_path):
            os.unlink(output_path)
        return SyncResult(success=False, error=str(e))


def _parse_offset(output):
    match = OFFSET_RE.search(output)
    if match:
        return float(match.group(1))
    return None


def _parse_framerate(output):
    match = FRAMERATE_RE.search(output)
    if match:
        return float(match.group(1))
    return None


def _parse_score(output):
    match = SCORE_RE.search(output)
    if match:
        return float(match.group(1))
    return None


def _estimate_confidence(offset, framerate):
    score = 1.0
    if offset is not None:
        abs_offset = abs(offset)
        if abs_offset > 30:
            score -= 0.5
        elif abs_offset > 10:
            score -= 0.3
        elif abs_offset > 3:
            score -= 0.1
    if framerate is not None and framerate != 1.0:
        deviation = abs(framerate - 1.0)
        if deviation > 0.05:
            score -= 0.3
        elif deviation > 0.01:
            score -= 0.1
    return max(0.0, min(1.0, score))
