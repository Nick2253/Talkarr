import logging
import os
import re

from .db import SubType

log = logging.getLogger(__name__)

FORCED_PATTERNS = [
    r"\.forced\.",
    r"\.foreign\.",
    r"\.signs?\.",
]

HI_PATTERNS = [
    r"\.hi\.",
    r"\.sdh\.",
    r"\.cc\.",
    r"\.hearing[_-]?impaired\.",
]

FORCED_RE = re.compile("|".join(FORCED_PATTERNS), re.IGNORECASE)
HI_RE = re.compile("|".join(HI_PATTERNS), re.IGNORECASE)


def classify(subtitle_path, language_tag=None):
    """Classify a subtitle as regular, forced, or HI.

    Uses the filename and optionally the Bazarr language tag
    (e.g. "en", "en:forced", "en:hi").
    """
    basename = os.path.basename(subtitle_path)

    if language_tag:
        tag = language_tag.lower()
        if ":forced" in tag or ":foreign" in tag:
            log.info("Classified %s as forced (language tag: %s)", basename, language_tag)
            return SubType.FORCED
        if ":hi" in tag or ":sdh" in tag:
            log.info("Classified %s as HI (language tag: %s)", basename, language_tag)
            return SubType.HI

    if FORCED_RE.search(basename):
        log.info("Classified %s as forced (filename pattern)", basename)
        return SubType.FORCED

    if HI_RE.search(basename):
        log.info("Classified %s as HI (filename pattern)", basename)
        return SubType.HI

    log.info("Classified %s as regular", basename)
    return SubType.REGULAR
