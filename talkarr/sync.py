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
        confirmed=None,
        measured=False,
        parts=None,
    ):
        self.success = success
        self.framerate = framerate
        # Whether syncing parts of the subtitle on their own bore the correction out
        # (None: not checked); whether it was measured from the output, not the log.
        self.confirmed = confirmed
        self.measured = measured
        self.parts = parts  # offsets the thirds measured on their own
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


# 23.976 -> 24 is x1.001; ffsubsync's output measures it as 1.000999 or so.
FRAMERATE_CHANGE = 5e-4


def changes_framerate(scale):
    return scale is not None and abs(scale - 1.0) > FRAMERATE_CHANGE


def run_ffsubsync(
    subtitle_path,
    media_path,
    output_path,
    max_offset=120,
    timeout=600,
    fix_framerate=True,
    serialize_speech=False,
):
    """Run ffsubsync, writing the synced subtitle to ``output_path``.

    The input is never modified. On failure ``output_path`` is removed.
    ``serialize_speech`` saves the reference's speech next to ``media_path`` (as
    ``.npz``, which later runs can take as their reference instead of the video).
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
    if serialize_speech:
        cmd.append("--serialize-speech")
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
        measured=measured is not None,
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


# A sync counts as confirmed when each third of the subtitle, synced on its own with
# offsets only, lands within this many seconds of where the correction puts it; with a
# framerate change, the drift between the first and last thirds must also be within
# this share of the one predicted.
CONFIRM_MAX_RESIDUAL = 1.0
DRIFT_TOLERANCE = 0.4
CONFIRM_MIN_CUES = 10


# When the thirds disagree with ffsubsync but agree with each other this closely, their
# shared offset is the correction (near zero: the subtitle is already in sync).
PARTS_AGREE = 0.5


def confirm_sync(subtitle_path, reference, scale, offset, scratch_dir):
    """Whether the thirds of the subtitle, each synced on its own (offsets only), agree
    with ``after = scale * before + offset``. ``None`` when the check can't run.

    ``reference``: the video, or the ``.npz`` speech ffsubsync saved from it (cheap).

    Library checks: real 23.976 <-> 24 changes and large offsets (The Good Place -89 s,
    Saving Private Ryan -41 s) agree within 0.4 s in every third; wrong-release
    subtitles and wrong framerate guesses scatter by tens of seconds."""
    return parts_confirm(measure_parts(subtitle_path, reference, scale, offset, scratch_dir), scale)


def measure_parts(subtitle_path, reference, scale, offset, scratch_dir):
    """``[(mean cue start, predicted offset, measured offset)]`` for the subtitle's
    thirds, each synced on its own with offsets only; ``None`` if that can't be done."""
    from .forced import subtitles as S

    try:
        cues = S.load(subtitle_path)
    except (OSError, ValueError):
        return None
    n = len(cues) // 3
    if n < CONFIRM_MIN_CUES:
        return None
    thirds = (cues[:n], cues[n : 2 * n], cues[-n:])
    times = [sum(c.start for c in p) / n for p in thirds]
    predicted = [(scale - 1.0) * t + offset for t in times]
    # A 25 -> 23.976 change moves the end of a film by minutes.
    window = max(120, int(max(abs(p) for p in predicted)) + 30)
    measured = []
    for part in thirds:
        fd, src = tempfile.mkstemp(suffix=".srt", dir=scratch_dir)
        os.close(fd)
        out = src[:-4] + ".out.srt"
        try:
            with open(src, "w", encoding="utf-8") as f:
                f.write(S.compose(part))
            result = run_ffsubsync(src, reference, out, max_offset=window, fix_framerate=False)
        finally:
            _remove(src)
            _remove(out)
        if not result.success or result.offset is None:
            return None
        measured.append(result.offset)
    return list(zip(times, predicted, measured, strict=True))


def parts_confirm(parts, scale):
    if parts is None:
        return None
    if any(abs(p - m) > CONFIRM_MAX_RESIDUAL for _, p, m in parts):
        return False
    if not changes_framerate(scale):
        return True
    (t1, _, m1), (t3, _, m3) = parts[0], parts[2]
    implied = 1.0 + (m3 - m1) / (t3 - t1)
    return abs(implied - scale) <= DRIFT_TOLERANCE * abs(scale - 1.0)


# Two of the three thirds this far out on their own, with no one correction between
# them: the subtitle doesn't match the video (another release or cut). One third in
# sync isn't enough (30 Rock: +0.9 s, +118 s, -68 s); two are (a different cold open).
MISMATCH_MIN_OFFSET = 1.0
MISMATCH_MIN_PARTS = 2


def parts_scale(parts):
    """``(scale, offset)`` for a standard framerate change all three thirds fit (each
    within CONFIRM_MAX_RESIDUAL) when ffsubsync guessed a different one; else None."""
    from .forced.fit import SCALES

    if not parts:
        return None
    best = None
    for scale in SCALES:
        if not changes_framerate(scale):
            continue
        offset = sum(m - (scale - 1.0) * t for t, _, m in parts) / len(parts)
        worst = max(abs((scale - 1.0) * t + offset - m) for t, _, m in parts)
        if worst <= CONFIRM_MAX_RESIDUAL and (best is None or worst < best[0]):
            best = (worst, scale, round(offset, 3))
    return best and best[1:]


def parts_mismatch(parts):
    out = sum(abs(m) > MISMATCH_MIN_OFFSET for _, _, m in parts or ())
    return out >= MISMATCH_MIN_PARTS


def parts_offset(parts):
    """The offset the thirds share, if they agree with each other; else ``None``."""
    measured = [m for _, _, m in parts or ()]
    if not measured or max(measured) - min(measured) > PARTS_AGREE:
        return None
    return round(sum(measured) / len(measured), 3)


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
