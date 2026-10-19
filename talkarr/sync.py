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
        framerate_confirmed=None,
    ):
        self.success = success
        self.framerate = framerate
        self.framerate_confirmed = framerate_confirmed
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

    result = run_ffsubsync(subtitle_path, media_path, output_path, max_offset=max_offset)
    if not result.success:
        return result

    try:
        replace_preserving_metadata(output_path, subtitle_path)
    except Exception as e:  # noqa: BLE001 - reported on the job, never fatal
        _remove(output_path)
        return SyncResult(success=False, error=str(e))

    log.info("Sync complete: offset=%.2fs, confidence=%.2f", result.offset or 0, result.confidence)
    result.output_path = subtitle_path
    return result


def run_ffsubsync(
    subtitle_path, media_path, output_path, max_offset=120, timeout=600, fix_framerate=True
):
    """Run ffsubsync, writing the synced subtitle to ``output_path``.

    The input is never modified. On failure ``output_path`` is removed.
    """
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
    if not fix_framerate:
        cmd.append("--no-fix-framerate")
    log.info(
        "Running ffsubsync: %s → %s",
        os.path.basename(subtitle_path),
        os.path.basename(media_path),
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        _remove(output_path)
        return SyncResult(success=False, error=f"ffsubsync timed out ({timeout}s)")
    except Exception as e:  # noqa: BLE001 - reported on the job, never fatal
        _remove(output_path)
        return SyncResult(success=False, error=str(e))

    output = result.stderr + result.stdout
    if result.returncode != 0:
        _remove(output_path)
        return SyncResult(
            success=False,
            error=f"ffsubsync exit {result.returncode}: {result.stderr[-500:].strip()}",
        )
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        _remove(output_path)
        return SyncResult(success=False, error="ffsubsync produced empty output")

    offset = _parse_offset(output)
    framerate = _parse_framerate(output)
    # ffsubsync prints both rounded ("0.959"), which is up to 1.5 s off across a film
    # with a framerate change; measure the exact correction from its output instead.
    measured = measure_correction(subtitle_path, output_path)
    if measured is not None:
        framerate, offset = measured
    return SyncResult(
        success=True,
        offset=offset,
        confidence=_estimate_confidence(offset, framerate),
        output_path=output_path,
        framerate=framerate,
        score=_parse_score(output),
    )


def measure_correction(before_path, after_path):
    """``(scale, offset)`` with ``after = scale * before + offset`` for each cue start,
    fitted by least squares, or ``None`` if the files don't pair up cue for cue."""
    from .forced import subtitles as S

    try:
        before, after = S.load(before_path), S.load(after_path)
    except (OSError, ValueError):
        return None
    if len(before) < 2 or len(before) != len(after):
        return None
    xs, ys = [c.start for c in before], [c.start for c in after]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return None
    scale = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
    if abs(scale - 1.0) < 1e-4:
        scale = 1.0
    offset = my - scale * mx
    return scale, round(offset, 3)


# A framerate change counts as confirmed when the first and last thirds of the subtitle,
# synced on their own with offsets only, see the drift it predicts: each within this
# many seconds of the prediction, and the drift between them within this share of it.
DRIFT_MAX_RESIDUAL = 1.0
DRIFT_TOLERANCE = 0.4
DRIFT_MIN_CUES = 10


def confirm_framerate(subtitle_path, media_path, scale, offset, scratch_dir):
    """Whether syncing the first and last thirds separately, with offsets only, agrees
    with ``after = scale * before + offset``. ``None`` when the check can't run.

    On 13 library subtitles with ffsubsync's 23.976 <-> 24 changes, 11 agreed to within
    0.2 s at both ends; the two that didn't (a disagreeing episode, a 25 fps guess that
    put the end 170 s out) were wrong."""
    from .forced import subtitles as S

    try:
        cues = S.load(subtitle_path)
    except (OSError, ValueError):
        return None
    n = len(cues) // 3
    if n < DRIFT_MIN_CUES:
        return None
    measured = []
    for part in (cues[:n], cues[-n:]):
        fd, src = tempfile.mkstemp(suffix=".srt", dir=scratch_dir)
        os.close(fd)
        out = src[:-4] + ".out.srt"
        try:
            with open(src, "w", encoding="utf-8") as f:
                f.write(S.compose(part))
            result = run_ffsubsync(src, media_path, out, fix_framerate=False)
        finally:
            _remove(src)
            _remove(out)
        if not result.success or result.offset is None:
            return None
        measured.append((sum(c.start for c in part) / n, result.offset))
    (t1, o1), (t2, o2) = measured
    for t, o in measured:
        if abs((scale - 1.0) * t + offset - o) > DRIFT_MAX_RESIDUAL:
            return False
    implied = 1.0 + (o2 - o1) / (t2 - t1)
    return abs(implied - scale) <= DRIFT_TOLERANCE * abs(scale - 1.0)


def _remove(path):
    if os.path.exists(path):
        os.unlink(path)


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
