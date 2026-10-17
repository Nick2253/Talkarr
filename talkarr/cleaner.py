"""Subtitle cleaning: encoding repair, BOM removal, and ad/credit stripping.

Cleaning never empties a file: if the ad rules would remove more than
``MAX_REMOVAL_RATIO`` of the cues, nothing is removed and the result carries an
error so the job can be flagged instead.
"""

import logging
import os
import re

import charset_normalizer
import ftfy
import pysrt

from .fileio import atomic_write_text

log = logging.getLogger(__name__)

# Provider watermarks and spam: removed wherever they appear.
STRONG_AD_PATTERNS = [
    r"opensubtitles",
    r"subscene",
    r"addic7ed",
    r"podnapisi",
    r"subdivx",
    r"yifysubtitles",
    r"advertise\s+your\s+product",
    r"support\s+us\s+and\s+become\s+vip",
    r"please\s+rate\s+this\s+subtitle",
]

# Credits and links: only removed near the start/end of the file, where release
# credits live, so dialogue that happens to mention a URL is kept.
CREDIT_PATTERNS = [
    r"subtitles?\s*by\b",
    r"ripped\s*by\b",
    r"synced?\s*(&|and)\s*corrected\s*by\b",
    r"captioned\s+by\b",
    r"encoded\s+by\b",
    r"subtitling\s+by\b",
    r"translat(ed|ion)\s*(and\s+subtitles\s*)?by\b",
    r"www\.\S+\.(com|org|net|io)",
    r"https?://\S+",
]

STRONG_AD_RE = re.compile("|".join(STRONG_AD_PATTERNS), re.IGNORECASE)
CREDIT_RE = re.compile("|".join(CREDIT_PATTERNS), re.IGNORECASE)

# Credit rules apply to this many cues at each end of the file.
CREDIT_EDGE_CUES = 3
MAX_REMOVAL_RATIO = 0.2

UTF8_BOM = b"\xef\xbb\xbf"


class CleanResult:
    def __init__(
        self,
        ads_removed=0,
        encoding_fixed=False,
        bom_stripped=False,
        cues_before=0,
        cues_after=0,
        error=None,
        source_encoding="utf-8",
    ):
        self.ads_removed = ads_removed
        self.encoding_fixed = encoding_fixed
        self.bom_stripped = bom_stripped
        self.cues_before = cues_before
        self.cues_after = cues_after
        self.error = error
        self.source_encoding = source_encoding

    @property
    def success(self):
        return self.error is None

    @property
    def reencoded(self):
        return self.source_encoding != "utf-8"

    @property
    def changed(self):
        return self.ads_removed > 0 or self.encoding_fixed or self.bom_stripped or self.reencoded


def clean_subtitle(subtitle_path):
    """Clean a subtitle file in place: fix encoding, strip BOM, remove ads.

    The file is rewritten as UTF-8 (no BOM) only if something changed.
    """
    if not os.path.isfile(subtitle_path):
        return CleanResult(error=f"File not found: {subtitle_path}")

    try:
        with open(subtitle_path, "rb") as f:
            raw = f.read()

        bom_stripped = raw.startswith(UTF8_BOM)
        if bom_stripped:
            raw = raw[len(UTF8_BOM) :]

        decoded = decode_subtitle(raw)
        if decoded is None:
            return CleanResult(error="Could not detect encoding")
        text, source_encoding = decoded

        repaired = ftfy.fix_encoding(text)
        encoding_fixed = repaired != text
        text = repaired

        subs = pysrt.from_string(text)
        cues_before = len(subs)

        to_remove = [i for i, sub in enumerate(subs) if _is_ad(sub, i, cues_before)]

        if to_remove and len(to_remove) > cues_before * MAX_REMOVAL_RATIO:
            return CleanResult(
                cues_before=cues_before,
                cues_after=cues_before,
                source_encoding=source_encoding,
                error=(
                    f"Refusing to remove {len(to_remove)}/{cues_before} "
                    f"cues as ads (limit {MAX_REMOVAL_RATIO:.0%})"
                ),
            )

        for i in reversed(to_remove):
            del subs[i]
        subs.clean_indexes()

        result = CleanResult(
            ads_removed=len(to_remove),
            encoding_fixed=encoding_fixed,
            bom_stripped=bom_stripped,
            cues_before=cues_before,
            cues_after=len(subs),
            source_encoding=source_encoding,
        )

        if result.changed:
            atomic_write_text(subtitle_path, _compose(subs))
            log.info(
                "Cleaned %s: %d ads removed, encoding_fixed=%s, source_encoding=%s",
                os.path.basename(subtitle_path),
                result.ads_removed,
                encoding_fixed,
                source_encoding,
            )

        return result

    except Exception as e:  # noqa: BLE001 - reported on the job, never fatal
        log.exception("Cleaning failed for %s", subtitle_path)
        return CleanResult(error=str(e))


def decode_subtitle(raw):
    """Decode subtitle bytes. Returns ``(text, encoding)`` or ``None``."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    matches = list(charset_normalizer.from_bytes(raw))
    if matches:
        best = matches[0]
        # Western text often decodes equally well as cp1250 (Central European)
        # and cp1252, and charset-normalizer lists cp1250 first, which turns
        # "crème" into "crčme". cp1252 is by far the most common legacy
        # subtitle encoding, so it wins ties.
        tied = [m for m in matches if m.chaos == best.chaos and m.coherence >= best.coherence]
        chosen = next((m for m in tied if m.encoding == "cp1252"), best)
        return str(chosen), chosen.encoding

    # cp1252 maps almost every byte, so this is a last resort.
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return None


def _compose(subs):
    return "\n".join(str(sub) for sub in subs)


def _is_ad(sub, index, total):
    text = sub.text.strip()
    if not text:
        return True

    if STRONG_AD_RE.search(text):
        return True

    near_edge = index < CREDIT_EDGE_CUES or index >= total - CREDIT_EDGE_CUES
    return near_edge and bool(CREDIT_RE.search(text))
