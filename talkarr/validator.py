import logging
import os

import pysrt

from .db import SubType

log = logging.getLogger(__name__)

FORCED_MAX_CUES = 100
REGULAR_MIN_CUES = 20
MIN_CUE_DURATION_MS = 200
MAX_CUE_DURATION_MS = 30_000
MAX_GAP_REGULAR_MS = 600_000


class ValidationResult:
    def __init__(self):
        self.score = 1.0
        self.issues = []
        self.warnings = []

    @property
    def passed(self):
        return self.score >= 0.4 and not self.issues

    def add_issue(self, msg, penalty=0.3):
        self.issues.append(msg)
        self.score = max(0.0, self.score - penalty)

    def add_warning(self, msg, penalty=0.1):
        self.warnings.append(msg)
        self.score = max(0.0, self.score - penalty)


def validate(subtitle_path, sub_type, sync_confidence=None):
    """Validate a subtitle file for quality issues."""
    result = ValidationResult()

    if not os.path.isfile(subtitle_path):
        result.add_issue("File not found")
        return result

    try:
        subs = pysrt.open(subtitle_path, encoding="utf-8")
    except Exception:
        try:
            subs = pysrt.open(subtitle_path)
        except Exception as e:
            result.add_issue(f"Cannot parse subtitle: {e}")
            return result

    cue_count = len(subs)
    if cue_count == 0:
        result.add_issue("Subtitle file is empty (0 cues)")
        return result

    _check_cue_count(result, cue_count, sub_type)
    _check_durations(result, subs)
    _check_gaps(result, subs, sub_type)
    _check_mislabel(result, cue_count, sub_type)

    if sync_confidence is not None and sync_confidence < 0.4:
        result.add_warning(
            f"Low sync confidence: {sync_confidence:.2f}", penalty=0.2)

    log.info("Validated %s: score=%.2f, issues=%d, warnings=%d",
             os.path.basename(subtitle_path), result.score,
             len(result.issues), len(result.warnings))

    return result


def _check_cue_count(result, count, sub_type):
    if sub_type == SubType.FORCED and count > FORCED_MAX_CUES:
        result.add_warning(
            f"Forced sub has {count} cues (expected <{FORCED_MAX_CUES})")
    elif sub_type == SubType.REGULAR and count < REGULAR_MIN_CUES:
        result.add_warning(
            f"Regular sub has only {count} cues (expected >{REGULAR_MIN_CUES})")


def _check_durations(result, subs):
    short = 0
    long_ = 0
    for sub in subs:
        dur_ms = _duration_ms(sub)
        if dur_ms < MIN_CUE_DURATION_MS:
            short += 1
        elif dur_ms > MAX_CUE_DURATION_MS:
            long_ += 1

    total = len(subs)
    if short > total * 0.1:
        result.add_warning(f"{short}/{total} cues shorter than {MIN_CUE_DURATION_MS}ms")
    if long_ > total * 0.05:
        result.add_warning(f"{long_}/{total} cues longer than {MAX_CUE_DURATION_MS}ms")


def _check_gaps(result, subs, sub_type):
    if sub_type == SubType.FORCED:
        return

    big_gaps = 0
    for i in range(1, len(subs)):
        gap = _start_ms(subs[i]) - _end_ms(subs[i - 1])
        if gap > MAX_GAP_REGULAR_MS:
            big_gaps += 1

    if big_gaps > 3:
        result.add_warning(
            f"{big_gaps} gaps longer than {MAX_GAP_REGULAR_MS // 1000}s "
            f"in regular subtitle")


def _check_mislabel(result, cue_count, sub_type):
    if sub_type == SubType.FORCED and cue_count > 200:
        result.add_issue(
            f"Likely mislabeled: {cue_count} cues in forced sub "
            f"(probably a regular sub)")
    elif sub_type == SubType.REGULAR and cue_count < 5:
        result.add_warning(
            f"Suspiciously few cues ({cue_count}) for a regular subtitle")


def _duration_ms(sub):
    return (_time_ms(sub.end) - _time_ms(sub.start))


def _start_ms(sub):
    return _time_ms(sub.start)


def _end_ms(sub):
    return _time_ms(sub.end)


def _time_ms(t):
    return (t.hours * 3600 + t.minutes * 60 + t.seconds) * 1000 + t.milliseconds
